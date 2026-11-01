import { afterEach, describe, expect, it, vi } from 'vitest'
import { systemSettingsApi } from './systemSettings'
import { authApi } from './auth'
import { setUnauthorizedHandler } from './client'

afterEach(() => { vi.unstubAllGlobals(); setUnauthorizedHandler(null) })
describe('系统设置API', () => {
  it('GET/PATCH沿用Cookie及AbortSignal，false/null/未提交字段保持语义', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => { calls.push({ url, init }); return new Response('{}') })
    const abort = new AbortController()
    await systemSettingsApi.get(abort.signal)
    await systemSettingsApi.update({ revision: 3, changes: { registration_enabled: false, task_max_concurrency: null } }, abort.signal)
    await authApi.config(abort.signal)
    expect(calls.map((call) => call.url)).toEqual(['/api/v1/admin/system-settings', '/api/v1/admin/system-settings', '/api/v1/auth/config'])
    expect(calls.every((call) => call.init.credentials === 'include' && call.init.signal === abort.signal)).toBe(true)
    expect(calls[1].init.method).toBe('PATCH')
    expect(JSON.parse(String(calls[1].init.body))).toEqual({ revision: 3, changes: { registration_enabled: false, task_max_concurrency: null } })
  })
  it.each([401, 403, 409, 422, 500])('%s不伪造成功、不自动重发，401仍清会话', async (status) => {
    const on401 = vi.fn()
    setUnauthorizedHandler(on401)
    const fetch = vi.fn(async () => new Response(JSON.stringify({ detail: '安全说明' }), { status }))
    vi.stubGlobal('fetch', fetch)
    await expect(systemSettingsApi.update({ revision: 0, changes: { mcp_enabled: false } })).rejects.toMatchObject({ status, detail: '安全说明' })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(on401).toHaveBeenCalledTimes(status === 401 ? 1 : 0)
  })
})
