import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, setUnauthorizedHandler } from './client'

interface Call {
  url: string
  init: RequestInit
}

function stubFetch(status: number, text: string | null): Call[] {
  const calls: Call[] = []
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    calls.push({ url, init })
    return new Response(text, { status })
  })
  return calls
}

afterEach(() => {
  vi.unstubAllGlobals()
  setUnauthorizedHandler(null)
})

describe('client', () => {
  it('带 credentials、/api/v1 前缀；JSON body 设置 Content-Type', async () => {
    const calls = stubFetch(200, '{"ok":true}')
    await expect(api.post('/projects', { a: 1 })).resolves.toEqual({ ok: true })
    expect(calls[0].url).toBe('/api/v1/projects')
    expect(calls[0].init.credentials).toBe('include')
    expect((calls[0].init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
  })

  it('FormData body 原样发送，不设置 Content-Type（由浏览器带 boundary）', async () => {
    const calls = stubFetch(200, '{}')
    const form = new FormData()
    form.append('file', new File(['x'], 'a.txt'))
    await api.upload('/projects/1/kb/documents', form)
    expect(calls[0].init.body).toBe(form)
    expect((calls[0].init.headers as Record<string, string>)['Content-Type']).toBeUndefined()
  })

  it('错误 detail 与非 JSON 回退', async () => {
    stubFetch(400, '{"detail":"x"}')
    await expect(api.get('/a')).rejects.toMatchObject({ status: 400, detail: 'x' })
    stubFetch(500, 'oops')
    const err = await api.get('/a').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).detail).toBe('请求失败（HTTP 500）')
  })

  it('204 → undefined', async () => {
    stubFetch(204, null)
    await expect(api.del('/a')).resolves.toBeUndefined()
  })

  it('401：普通路径调用处理器，登录类路径不调用', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    stubFetch(401, '{"detail":"未登录或登录已过期"}')
    await api.get('/projects').catch(() => undefined)
    expect(handler).toHaveBeenCalledTimes(1)
    await api.post('/auth/login', {}).catch(() => undefined)
    await api.get('/auth/me').catch(() => undefined)
    expect(handler).toHaveBeenCalledTimes(1)
  })
})
