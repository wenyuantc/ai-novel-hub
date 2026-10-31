import { afterEach, describe, expect, it, vi } from 'vitest'
import { AUDIT_RESULT, auditLogApi, auditResult } from './auditLogs'
import { ApiError, setUnauthorizedHandler } from './client'

function stubFetch(status = 200, body: unknown = { items: [], total: 0 }) {
  const calls: { url: string; init: RequestInit }[] = []
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    calls.push({ url, init })
    return new Response(JSON.stringify(body), { status })
  })
  return calls
}

afterEach(() => { vi.unstubAllGlobals(); setUnauthorizedHandler(null) })

describe('管理员审计 API', () => {
  it('只有task.cancel_request成功局部标为已受理，全局success仍为成功', () => {
    expect(auditResult({ action: 'task.cancel_request', result: 'success' }).label).toBe('已受理')
    expect(auditResult({ action: 'chapter.update', result: 'success' }).label).toBe('成功')
    expect(auditResult({ action: 'task.cancel_request', result: 'failure' }).label).toBe('失败')
    expect(AUDIT_RESULT.success.label).toBe('成功')
  })
  it('组合筛选、稳定分页参数与历史用户名普通文本编码', async () => {
    const calls = stubFetch()
    await expect(auditLogApi.list({ actor_user_id: 7, actor_q: '  已注销%_\\用户  ', target_type: 'chapter', target_id: 19, action: 'chapter.update', result: 'failure', from_at: '2026-10-29T16:00:00.000Z', to_at: '2026-10-31T16:00:00.000Z', page: 2, page_size: 50 })).resolves.toEqual({ items: [], total: 0 })
    const url = new URL(calls[0].url, 'http://test')
    expect(url.pathname).toBe('/api/v1/admin/audit-logs')
    expect(Object.fromEntries(url.searchParams)).toEqual({ actor_user_id: '7', actor_q: '已注销%_\\用户', target_type: 'chapter', target_id: '19', action: 'chapter.update', result: 'failure', from_at: '2026-10-29T16:00:00.000Z', to_at: '2026-10-31T16:00:00.000Z', page: '2', page_size: '50' })
    expect(calls[0].init.credentials).toBe('include')
    await auditLogApi.list({ actor_q: '   ' })
    expect(calls[1].url).toBe('/api/v1/admin/audit-logs')
  })

  it('详情、独立日期预览、二次确认请求，不发送列表筛选或自由清空选项', async () => {
    const calls = stubFetch()
    const range = { from_at: '2026-10-29T16:00:00Z', to_at: '2026-10-30T16:00:00Z' }
    await auditLogApi.get(12)
    await auditLogApi.previewCleanup(range)
    await auditLogApi.cleanup({ preview_token: 'short-lived-preview', confirm: true })
    expect(calls.map(({ url, init }) => [url, init.method, init.body])).toEqual([
      ['/api/v1/admin/audit-logs/12', 'GET', undefined],
      ['/api/v1/admin/audit-logs/cleanup-preview', 'POST', JSON.stringify(range)],
      ['/api/v1/admin/audit-logs/cleanup', 'POST', JSON.stringify({ preview_token: 'short-lived-preview', confirm: true })],
    ])
    expect(calls.every(({ init }) => init.credentials === 'include')).toBe(true)
  })

  it.each([400, 403, 404, 409, 500])('HTTP %s 的安全 detail 和错误类别完整保留', async (status) => {
    stubFetch(status, { detail: '请重新预览', code: 'preview_replayed' })
    await expect(auditLogApi.cleanup({ preview_token: 'old', confirm: true })).rejects.toMatchObject({ status, detail: '请重新预览', body: { code: 'preview_replayed' } })
  })

  it('401 触发统一登出，普通用户 403 不伪装为前端授权', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    stubFetch(401, { detail: '请先登录' })
    await expect(auditLogApi.list()).rejects.toBeInstanceOf(ApiError)
    expect(handler).toHaveBeenCalledOnce()
    stubFetch(403, { detail: '需要管理员权限' })
    await expect(auditLogApi.previewCleanup({ from_at: '2026-10-30T00:00:00Z', to_at: '2026-10-31T00:00:00Z' })).rejects.toMatchObject({ status: 403 })
    expect(handler).toHaveBeenCalledOnce()
  })
})
