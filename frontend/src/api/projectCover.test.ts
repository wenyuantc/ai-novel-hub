import { afterEach, describe, expect, it, vi } from 'vitest'
import { setUnauthorizedHandler } from './client'
import { coverPrompt, coverUrl, deleteCover, generateCover, uploadCover } from './projects'

afterEach(() => {
  vi.unstubAllGlobals()
  setUnauthorizedHandler(null)
})

describe('封面接口', () => {
  it('无封面时不请求图片，更新版本后 URL 改变且保留原始精度', () => {
    expect(coverUrl({ id: 3, cover_updated_at: null })).toBeNull()
    const before = coverUrl({ id: 3, cover_updated_at: '2026-10-29T10:00:00.123456' })
    const after = coverUrl({ id: 3, cover_updated_at: '2026-10-29T10:00:00.123457' })
    expect(before).toBe('/api/v1/projects/3/cover?v=2026-10-29T10%3A00%3A00.123456')
    expect(after).not.toBe(before)
  })

  it('上传使用 PUT multipart，携带 Cookie 且不手动设置 Content-Type', async () => {
    const response = { id: 3, cover_updated_at: '2026-10-29T10:00:00' }
    const fetchMock = vi.fn(async (url: string, init: RequestInit) => {
      expect(url).toBe('/api/v1/projects/3/cover')
      expect(init.method).toBe('PUT')
      return new Response(JSON.stringify(response))
    })
    vi.stubGlobal('fetch', fetchMock)
    const blob = new Blob(['picture'], { type: 'image/png' })
    await expect(uploadCover(3, blob)).resolves.toEqual(response)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/projects/3/cover')
    expect(init.method).toBe('PUT')
    expect(init.credentials).toBe('include')
    const headers = new Headers(init.headers)
    expect(headers.has('content-type')).toBe(false)
    expect(init.body).toBeInstanceOf(FormData)
    const file = (init.body as FormData).get('file') as File
    expect(file.type).toBe('image/png')
    expect(await file.text()).toBe('picture')
  })

  it('提示词、生成、删除分别走既定接口；生成传递取消信号且不调用上传', async () => {
    const calls: { url: string; init: RequestInit }[] = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      calls.push({ url, init })
      return new Response(JSON.stringify({ prompt: '竖版封面' }))
    })
    const controller = new AbortController()
    await coverPrompt(3)
    await generateCover(3, { prompt: '画一片星海', config_id: 7 }, controller.signal)
    await deleteCover(3)
    expect(calls.map(({ url, init }) => [init.method, url])).toEqual([
      ['GET', '/api/v1/projects/3/cover/prompt'],
      ['POST', '/api/v1/projects/3/cover/generate'],
      ['DELETE', '/api/v1/projects/3/cover'],
    ])
    expect(JSON.parse(calls[1].init.body as string)).toEqual({ prompt: '画一片星海', config_id: 7 })
    expect(calls[1].init.signal).toBe(controller.signal)
    expect(calls.every(({ init }) => init.credentials === 'include')).toBe(true)
  })

  it('后端中文错误原样展示，401 仍触发全局会话清理', async () => {
    const unauthorized = vi.fn()
    setUnauthorizedHandler(unauthorized)
    vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ detail: '封面不能超过 10MB' }), { status: 400 }))
    await expect(uploadCover(3, new Blob(['x']))).rejects.toMatchObject({ status: 400, detail: '封面不能超过 10MB' })
    vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ detail: '请先登录' }), { status: 401 }))
    await expect(deleteCover(3)).rejects.toMatchObject({ status: 401, detail: '请先登录' })
    expect(unauthorized).toHaveBeenCalledOnce()
  })
})
