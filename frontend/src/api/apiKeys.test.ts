import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiKeyApi } from './apiKeys'
import { setUnauthorizedHandler } from './client'

interface Call { url: string; init: RequestInit }

function stubFetch(status: number, body?: unknown): Call[] {
  const calls: Call[] = []
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    calls.push({ url, init })
    return new Response(status === 204 ? null : JSON.stringify(body), { status })
  })
  return calls
}

afterEach(() => {
  vi.unstubAllGlobals()
  setUnauthorizedHandler(null)
})

describe('apiKeyApi', () => {
  it('管理请求使用 Cookie，遵守创建、编辑、显密和撤销契约', async () => {
    const calls = stubFetch(204)
    const signal = new AbortController().signal
    await apiKeyApi.list()
    await apiKeyApi.create({ name: '阅读器', all_projects: false, project_ids: [3], expires_at: null })
    await apiKeyApi.update(7, { name: '同步工具', expires_at: null })
    await apiKeyApi.reveal(7, signal)
    await expect(apiKeyApi.revoke(7)).resolves.toBeUndefined()
    expect(calls.map(({ url, init }) => [init.method, url, init.body])).toEqual([
      ['GET', '/api/v1/api-keys', undefined],
      ['POST', '/api/v1/api-keys', JSON.stringify({ name: '阅读器', all_projects: false, project_ids: [3], expires_at: null })],
      ['PATCH', '/api/v1/api-keys/7', JSON.stringify({ name: '同步工具', expires_at: null })],
      ['POST', '/api/v1/api-keys/7/reveal', undefined],
      ['POST', '/api/v1/api-keys/7/revoke', undefined],
    ])
    expect(calls.every(({ init }) => init.credentials === 'include')).toBe(true)
    expect(calls[3].init.signal).toBe(signal)
  })

  it('管理端透传安全的中文错误，不把访问失败当成空密钥', async () => {
    stubFetch(400, { detail: '密钥已撤销，无法查看' })
    await expect(apiKeyApi.reveal(7)).rejects.toMatchObject({ status: 400, detail: '密钥已撤销，无法查看' })
  })

  it('管理端会话失效沿用全局登出行为', async () => {
    const expired = vi.fn()
    setUnauthorizedHandler(expired)
    stubFetch(401, { detail: '未登录或登录已过期' })
    await expect(apiKeyApi.list()).rejects.toMatchObject({ status: 401 })
    expect(expired).toHaveBeenCalledOnce()
  })
})
