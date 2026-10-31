import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, setUnauthorizedHandler } from './client'
import { recycleBinApi } from './recycleBin'

const deletedAt = '2026-10-31T01:02:03.123456Z'

function stubFetch(status: number, body?: unknown) {
  const calls: { url: string; init: RequestInit }[] = []
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    calls.push({ url, init })
    return new Response(status === 204 ? null : JSON.stringify(body), { status })
  })
  return calls
}

afterEach(() => { vi.unstubAllGlobals(); setUnauthorizedHandler(null) })

describe('管理员回收站 API', () => {
  it('列表按字面查询组合归属/分页，保留 Cookie，空白查询不发送', async () => {
    const calls = stubFetch(200, { items: [], total: 0 })
    await expect(recycleBinApi.list({ q: '  书%_\\名  ', owner_user_id: 7, page: 3, page_size: 50 })).resolves.toEqual({ items: [], total: 0 })
    const url = new URL(calls[0].url, 'http://test')
    expect(url.pathname).toBe('/api/v1/admin/recycle-bin/projects')
    expect(Object.fromEntries(url.searchParams)).toEqual({ q: '书%_\\名', owner_user_id: '7', page: '3', page_size: '50' })
    expect(calls[0].init.credentials).toBe('include')
    await recycleBinApi.list({ q: '   ' })
    expect(calls[1].url).toBe('/api/v1/admin/recycle-bin/projects')
  })
  it('恢复/彻底删除使用 POST；微秒删除代次和精确名称原样发送，204 不读取正文', async () => {
    const calls = stubFetch(204)
    await expect(recycleBinApi.restore(9, { deleted_at: deletedAt, confirm: true })).resolves.toBeUndefined()
    await expect(recycleBinApi.purge(9, { deleted_at: deletedAt, title: ' 星河 ', confirm: true })).resolves.toBeUndefined()
    expect(calls.map(({ url, init }) => [url, init.method, JSON.parse(String(init.body))])).toEqual([
      ['/api/v1/admin/recycle-bin/projects/9/restore', 'POST', { deleted_at: deletedAt, confirm: true }],
      ['/api/v1/admin/recycle-bin/projects/9/purge', 'POST', { deleted_at: deletedAt, title: ' 星河 ', confirm: true }],
    ])
    expect(calls.every(({ init }) => init.credentials === 'include')).toBe(true)
  })
  it('冲突/无权限保留服务器 detail，只有 401 触发全局登出', async () => {
    const unauthorized = vi.fn()
    setUnauthorizedHandler(unauthorized)
    stubFetch(409, { detail: '回收站记录已变化，请刷新后重试', code: 'recycle_record_stale' })
    const error = await recycleBinApi.restore(9, { deleted_at: deletedAt, confirm: true }).catch((err: unknown) => err)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 409, detail: '回收站记录已变化，请刷新后重试', body: { code: 'recycle_record_stale' } })
    stubFetch(403, { detail: '需要管理员权限' })
    await expect(recycleBinApi.list()).rejects.toMatchObject({ status: 403 })
    expect(unauthorized).not.toHaveBeenCalled()
    stubFetch(401, { detail: '未登录或登录已过期' })
    await expect(recycleBinApi.list()).rejects.toMatchObject({ status: 401 })
    expect(unauthorized).toHaveBeenCalledOnce()
  })
})
