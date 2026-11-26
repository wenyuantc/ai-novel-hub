import { afterEach, expect, it, vi } from 'vitest'
import { serializationApi } from './serialization'
import { setUnauthorizedHandler } from './client'

afterEach(() => {
  vi.unstubAllGlobals()
  setUnauthorizedHandler(null)
})

it('月历、日期和已删除章的发布详情均使用作品内资源', async () => {
  const send = vi.fn(async () => new Response('{}'))
  vi.stubGlobal('fetch', send)
  await serializationApi.overview(3, '2026-11')
  await serializationApi.day(3, '2026-11-18', { offset: 10, limit: 5 })
  await serializationApi.chapters(3, { stage: 'proofreading', stock_only: false })
  await serializationApi.chapter(3, 12, 9)
  await serializationApi.publications(3, { channel: '渠道&甲', offset: 0 })
  await serializationApi.publication(3, 20)
  const paths = vi.mocked(fetch).mock.calls.map(([url]) => new URL(String(url), 'http://localhost'))
  expect(paths.map(({ pathname }) => pathname.replace('/api/v1/projects/3/serialization', '')))
    .toEqual(['/overview', '/days/2026-11-18', '/chapters', '/chapters/12', '/publications', '/publications/20'])
  expect(paths[0].searchParams.get('month')).toBe('2026-11')
  expect(paths[1].searchParams.get('offset')).toBe('10')
  expect(paths[2].searchParams.get('stock_only')).toBe('false')
  expect(paths[3].searchParams.get('edition_id')).toBe('9')
  expect(paths[4].searchParams.get('channel')).toBe('渠道&甲')
})

it('稀疏编辑、明确撤销排期以及发布重试保留各自请求的原意和作者身份', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}')))
  const editorActor = { id: 1, createdAt: '2026-11-18T00:00:00Z' }
  const opts = { editorActor, isCurrent: () => true }
  await serializationApi.setGoal(3, { daily_goal: null, expected_revision: 2 }, opts)
  await serializationApi.updateEntry(3, 12, { expected_scene_id: 15, expected_revision: 4, stage: 'proofreading' }, opts)
  await serializationApi.updateEntry(3, 12, { expected_scene_id: 15, expected_revision: 5, planned_date: null }, opts)
  await serializationApi.finalize(3, 12, { expected_scene_id: 15, expected_revision: 6, expected_content_revision: 7, expected_fingerprint: 'a'.repeat(64) }, opts)
  const body = { expected_revision: 7, edition_id: 20, channel: '渠道甲', published_at: '2026-11-17T22:00:00Z', request_id: 'one-intent' }
  await serializationApi.publish(3, 12, body, opts)
  await serializationApi.publish(3, 12, body, opts)
  const calls = vi.mocked(fetch).mock.calls.map(([, init]) => init!)
  expect(calls.map(call => call.method)).toEqual(['PUT', 'PATCH', 'PATCH', 'POST', 'POST', 'POST'])
  expect(JSON.parse(String(calls[1].body))).toEqual({ expected_scene_id: 15, expected_revision: 4, stage: 'proofreading' })
  expect(JSON.parse(String(calls[2].body))).toEqual({ expected_scene_id: 15, expected_revision: 5, planned_date: null })
  expect(calls[4].body).toBe(calls[5].body)
  expect(calls[5].body).toBe(JSON.stringify(body))
  expect(calls[5].headers).toMatchObject({ 'X-Editor-Actor-Id': '1', 'X-Editor-Actor-Created-At': editorActor.createdAt })
})

it('按发布记录下载指定格式，blob 完成时检查最初作品和账号', async () => {
  let current = true
  const save = vi.fn()
  const fetcher = vi.fn(async () => ({
    ok: true, headers: new Headers(),
    blob: async () => {
      current = false
      return new Blob(['发布时的正文'])
    },
  }))
  vi.stubGlobal('fetch', fetcher)
  await expect(serializationApi.download(2, 31, 'md', { isCurrent: () => current, save })).resolves.toBeNull()
  expect(vi.mocked(fetch).mock.calls[0][0]).toBe('/api/v1/projects/2/serialization/publications/31/export?format=md')
  expect(save).not.toHaveBeenCalled()
})

it('失效作品的晚到详情错误不触发全局登出', async () => {
  const logout = vi.fn()
  setUnauthorizedHandler(logout)
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{"detail":"已退出"}', { status: 401 })))
  await expect(serializationApi.chapter(2, 3, undefined, { isCurrent: () => false })).rejects.toThrow('已退出')
  expect(logout).not.toHaveBeenCalled()
})

it('分页仅取核对清单，明确确认时保留原来的登记请求', async () => {
  const page = { confirmation_token: 'signed', total: 60, offset: 50, limit: 50, hits: [] }
  const send = vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ code: 'serialization_preflight_required', preflight: page }), { status: 409 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ code: 'stale_serialization_preflight', preflight: { ...page, total: 0 } }), { status: 409 }))
    .mockResolvedValueOnce(new Response('{"id":17}'))
  vi.stubGlobal('fetch', send)
  const request = { expected_revision: 7, edition_id: 20, channel: '渠道甲', published_at: '2026-11-17T22:00:00Z', request_id: 'one-intent' }
  await expect(serializationApi.publish(3, 12, request, undefined, 50)).resolves.toEqual({ kind: 'review', stale: false, page })
  const confirmed = { ...request, confirmation_token: 'signed' }
  await expect(serializationApi.publish(3, 12, confirmed)).resolves.toMatchObject({ kind: 'review', stale: true, page: { total: 0 } })
  await expect(serializationApi.publish(3, 12, confirmed)).resolves.toEqual({ kind: 'saved', result: { id: 17 } })
  expect(send.mock.calls[0][0]).toContain('/chapters/12/publications?preflight_offset=50')
  expect(JSON.parse(send.mock.calls[0][1].body)).toEqual(request)
  expect(send.mock.calls[1][0]).toMatch(/\/chapters\/12\/publications$/)
  expect(JSON.parse(send.mock.calls[1][1].body)).toEqual(confirmed)
})

it('按所选定稿下载 TXT，读取途中作用域失效则取消保存', async () => {
  let current = true
  const save = vi.fn()
  const raw = '第 3 章 · 旧章名\n\n中文🙂\n第二段'
  const fetcher = vi.fn()
    .mockResolvedValueOnce(new Response(raw))
    .mockResolvedValueOnce({ ok: true, headers: new Headers(), blob: async () => { current = false; return new Blob([raw]) } })
  vi.stubGlobal('fetch', fetcher)
  const opts = { isCurrent: () => current, save }
  await expect(serializationApi.downloadEdition(2, 3, 9, opts)).resolves.toMatchObject({ filename: '定稿-9.txt' })
  expect(await save.mock.calls[0][0].text()).toBe(raw)
  expect(fetcher.mock.calls[0][0]).toBe('/api/v1/projects/2/serialization/chapters/3/editions/9/export')
  await expect(serializationApi.downloadEdition(2, 3, 9, opts)).resolves.toBeNull()
  expect(save).toHaveBeenCalledTimes(1)
})
