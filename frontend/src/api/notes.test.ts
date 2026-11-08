import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './client'
import { isNoteConflict, noteApi } from './notes'

afterEach(() => vi.unstubAllGlobals())

describe('作品笔记 API', () => {
  it('分页查询和所有写入携带正确归属及修订号', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      calls.push({ url, init })
      return new Response(init.method === 'DELETE' ? null : '{}', { status: init.method === 'DELETE' ? 204 : 200 })
    })
    await noteApi.list(7, { q: '  青云山  ', page: 2, page_size: 20 })
    await noteApi.get(7, 3)
    await noteApi.create(7, { title: '笔记', content: '原文' })
    await noteApi.update(7, 3, { expected_revision: 5, content: '' })
    await noteApi.remove(7, 3, 6)
    expect(calls.map(({ url, init }) => [init.method, url])).toEqual([
      ['GET', '/api/v1/projects/7/notes?q=%E9%9D%92%E4%BA%91%E5%B1%B1&page=2&page_size=20'],
      ['GET', '/api/v1/projects/7/notes/3'], ['POST', '/api/v1/projects/7/notes'],
      ['PATCH', '/api/v1/projects/7/notes/3'], ['DELETE', '/api/v1/projects/7/notes/3?expected_revision=6'],
    ])
    expect(JSON.parse(calls[3].init.body as string)).toEqual({ expected_revision: 5, content: '' })
    expect(calls.every(({ init }) => init.credentials === 'include')).toBe(true)
  })

  it('收藏/固定筛选只在为 true 时携带，标记通过独立 PUT 设置且不带修订号', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      calls.push({ url, init })
      return new Response('{}', { status: 200 })
    })
    await noteApi.list(7, { q: '线索', favorite: true, page: 1, page_size: 20 })
    await noteApi.list(7, { pinned: true, page_size: 100 })
    await noteApi.list(7, { favorite: false, pinned: false })
    await noteApi.setFlags(7, 3, { is_pinned: true })
    await noteApi.setFlags(7, 3, { is_favorite: false })
    expect(calls.map(({ url, init }) => [init.method, url])).toEqual([
      ['GET', '/api/v1/projects/7/notes?q=%E7%BA%BF%E7%B4%A2&favorite=true&page=1&page_size=20'],
      ['GET', '/api/v1/projects/7/notes?pinned=true&page_size=100'],
      ['GET', '/api/v1/projects/7/notes'],
      ['PUT', '/api/v1/projects/7/notes/3/flags'], ['PUT', '/api/v1/projects/7/notes/3/flags'],
    ])
    expect(JSON.parse(calls[3].init.body as string)).toEqual({ is_pinned: true })
    expect(JSON.parse(calls[4].init.body as string)).toEqual({ is_favorite: false })
  })

  it('只识别修订冲突，不自动重试写入', async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify({ detail: '已修改', code: 'stale_revision' }), { status: 409 }))
    vi.stubGlobal('fetch', fetch)
    const error = await noteApi.update(1, 3, { expected_revision: 1, title: '新' }).catch((err: unknown) => err)
    expect(isNoteConflict(error)).toBe(true)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(isNoteConflict(new ApiError(409, '其他冲突'))).toBe(false)
  })
})
