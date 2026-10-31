import { afterEach, describe, expect, it, vi } from 'vitest'
import { adminDashboardApi } from './adminDashboard'
import { setUnauthorizedHandler } from './client'

afterEach(() => {
  vi.unstubAllGlobals()
  setUnauthorizedHandler(null)
})

describe('管理员首页 API', () => {
  it('三个只读请求使用冻结路径、默认30天、会话Cookie和AbortSignal', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      calls.push({ url, init })
      return new Response(JSON.stringify({ projects: 0, totals: null }), { status: 200 })
    })
    const controller = new AbortController()
    await expect(adminDashboardApi.overview(controller.signal)).resolves.toEqual({ projects: 0, totals: null })
    await adminDashboardApi.tasks(controller.signal)
    await adminDashboardApi.history()
    await adminDashboardApi.history(7, controller.signal)
    expect(calls.map(({ url }) => url)).toEqual([
      '/api/v1/admin/dashboard/overview', '/api/v1/admin/dashboard/tasks',
      '/api/v1/admin/dashboard/history?days=30', '/api/v1/admin/dashboard/history?days=7',
    ])
    expect(calls.every(({ init }) => init.method === 'GET' && init.credentials === 'include')).toBe(true)
    expect(calls[0].init.signal).toBe(controller.signal)
    expect(calls[1].init.signal).toBe(controller.signal)
    expect(calls[3].init.signal).toBe(controller.signal)
  })

  it('403不伪造成功值，401沿用全局会话失效处理', async () => {
    const unauthorized = vi.fn()
    setUnauthorizedHandler(unauthorized)
    vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ detail: '需要管理员权限' }), { status: 403 }))
    await expect(adminDashboardApi.overview()).rejects.toMatchObject({ status: 403, detail: '需要管理员权限' })
    expect(unauthorized).not.toHaveBeenCalled()
    vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ detail: '登录已过期' }), { status: 401 }))
    await expect(adminDashboardApi.tasks()).rejects.toMatchObject({ status: 401 })
    expect(unauthorized).toHaveBeenCalledTimes(1)
  })

  it('中止请求原样抛出AbortError，不转换成网络失败', async () => {
    vi.stubGlobal('fetch', async (_: string, init: RequestInit) => {
      expect(init.signal?.aborted).toBe(true)
      throw new DOMException('aborted', 'AbortError')
    })
    const controller = new AbortController()
    controller.abort()
    await expect(adminDashboardApi.history(90, controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
  })
})
