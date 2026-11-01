import { afterEach, describe, expect, it, vi } from 'vitest'
import { adminApi } from './admin'
import { authApi } from './auth'
import { ApiError, setUnauthorizedHandler } from './client'

interface Call {
  url: string
  init: RequestInit
}

function stubFetch(status: number, body?: unknown): Call[] {
  const calls: Call[] = []
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    calls.push({ url, init })
    const text = body === undefined ? null : JSON.stringify(body)
    return new Response(status === 204 ? null : text, { status })
  })
  return calls
}

afterEach(() => {
  vi.unstubAllGlobals()
  setUnauthorizedHandler(null)
})

describe('adminApi', () => {
  it('用户详情使用独立读取路径、Cookie 和可中止信号，null 登录及零用量不改写', async () => {
    const usage = { project_count: 0, chapter_count: 0, total_words: 0 }
    const body = { id: 7, username: 'alice', is_admin: false, is_active: true, created_at: '2026-10-30T00:00:00Z', project_count: 0, last_login_at: null, usage: { active: usage, recycled: usage, deleting: usage } }
    const calls = stubFetch(200, body)
    const controller = new AbortController()
    await expect(adminApi.getUser(7, controller.signal)).resolves.toEqual(body)
    expect(calls[0].url).toBe('/api/v1/admin/users/7')
    expect(calls[0].init.credentials).toBe('include')
    expect(calls[0].init.signal).toBe(controller.signal)
  })

  it('用户详情的 404/403 安全错误透传，401 沿用全局会话失效', async () => {
    for (const [status, detail] of [[404, '用户不存在'], [403, '需要管理员权限']] as const) {
      stubFetch(status, { detail })
      await expect(adminApi.getUser(7)).rejects.toMatchObject({ status, detail })
    }
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    stubFetch(401, { detail: '未登录或登录已过期' })
    await expect(adminApi.getUser(7)).rejects.toMatchObject({ status: 401 })
    expect(handler).toHaveBeenCalledOnce()
  })

  it('跨用户项目列表组合筛选与分页，查询词作为普通文本编码', async () => {
    const calls = stubFetch(200, { items: [], total: 0 })
    await expect(adminApi.listProjects({ q: '  书%_ ', owner_user_id: 7, status: 'writing', page: 2, page_size: 50 })).resolves.toEqual({ items: [], total: 0 })
    const url = new URL(calls[0].url, 'http://test')
    expect(url.pathname).toBe('/api/v1/admin/projects')
    expect(Object.fromEntries(url.searchParams)).toEqual({ q: '书%_', owner_user_id: '7', status: 'writing', page: '2', page_size: '50' })
    expect(calls[0].init.credentials).toBe('include')
    await adminApi.listProjects({ q: '  ' })
    expect(calls[1].url).toBe('/api/v1/admin/projects')
  })

  it('列表：分页与搜索参数，空搜索词不发送', async () => {
    const calls = stubFetch(200, { items: [], total: 0 })
    await expect(adminApi.listUsers({ q: '  ali ', page: 2, page_size: 20 })).resolves.toEqual({ items: [], total: 0 })
    await adminApi.listUsers({ q: '   ', page: 1, page_size: 20 })
    expect(calls[0].url).toBe('/api/v1/admin/users?q=ali&page=2&page_size=20')
    expect(calls[1].url).toBe('/api/v1/admin/users?page=1&page_size=20')
    expect(calls[0].init.credentials).toBe('include')
  })

  it('创建 / 更新 / 重置密码 / 删除的路径、方法与请求体', async () => {
    const calls = stubFetch(204)
    await adminApi.createUser({ username: 'bob', password: 'secret1', is_admin: true })
    await adminApi.updateUser(5, { is_active: false })
    await adminApi.resetPassword(5, { new_password: 'secret2' })
    await adminApi.deleteUser(5)
    expect(calls.map((c) => [c.init.method, c.url, c.init.body])).toEqual([
      ['POST', '/api/v1/admin/users', JSON.stringify({ username: 'bob', password: 'secret1', is_admin: true })],
      ['PATCH', '/api/v1/admin/users/5', JSON.stringify({ is_active: false })],
      ['POST', '/api/v1/admin/users/5/reset-password', JSON.stringify({ new_password: 'secret2' })],
      ['DELETE', '/api/v1/admin/users/5', undefined],
    ])
  })

  it('后端中文 detail 透传为 ApiError（409 重名、403 非管理员）', async () => {
    stubFetch(409, { detail: '用户名已存在' })
    await expect(adminApi.createUser({ username: 'bob', password: 'secret1' })).rejects.toMatchObject({
      status: 409,
      detail: '用户名已存在',
    })
    stubFetch(403, { detail: '需要管理员权限' })
    const err = await adminApi.listUsers().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).detail).toBe('需要管理员权限')
  })
})

describe('authApi.changePassword', () => {
  it('POST 旧/新密码，204 返回 undefined', async () => {
    const calls = stubFetch(204)
    await expect(authApi.changePassword({ old_password: 'old123', new_password: 'new123' })).resolves.toBeUndefined()
    expect(calls[0].url).toBe('/api/v1/auth/change-password')
    expect(calls[0].init.method).toBe('POST')
    expect(calls[0].init.body).toBe(JSON.stringify({ old_password: 'old123', new_password: 'new123' }))
  })

  it('原密码错误（400）不触发全局登出', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    stubFetch(400, { detail: '原密码不正确' })
    await expect(authApi.changePassword({ old_password: 'x', new_password: 'new123' })).rejects.toMatchObject({
      status: 400,
      detail: '原密码不正确',
    })
    expect(handler).not.toHaveBeenCalled()
  })
})
