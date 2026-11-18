import { afterEach, expect, it, vi } from 'vitest'
import { bookSearchApi, type ApplyRevision } from './bookSearch'

afterEach(() => vi.unstubAllGlobals())

it('全书搜索接口保存原样文本、分页快照、写入身份与同一重试标识', async () => {
  const fetcher = vi.fn().mockImplementation(() => Promise.resolve(new Response('{}')))
  vi.stubGlobal('fetch', fetcher)
  const signal = new AbortController().signal, current = () => true
  const actor = { id: 5, createdAt: '2026-11-18T00:00:00.123456' }
  const search = { query: ' 𠮷\n', start_chapter_id: 11, end_chapter_id: 15, page: 2, page_size: 20, snapshot: 'a'.repeat(64) }
  const body: ApplyRevision = { query: search.query, replacement: '', request_id: 'one-intent',
    hits: [{ chapter_id: 11, chapter_no: 2, chapter_title: '第一场雪', scene_id: 7, start: 5, end: 8, revision: 2, token: 'preview-token' }] }
  await bookSearchApi.query(2, search, signal, current)
  await bookSearchApi.preview(2, { ...search, replacement: '' }, signal, current)
  await bookSearchApi.apply(2, body, actor, current)
  await bookSearchApi.apply(2, body, actor, current)
  await bookSearchApi.history(2, 2, signal, current)
  await bookSearchApi.undo(2, 6, actor, current)
  expect(fetcher.mock.calls.map(([url]) => String(url).split('/book-search/')[1]))
    .toEqual(['query', 'preview', 'apply', 'apply', 'batches?page=2&page_size=20', 'batches/6/undo'])
  const init = fetcher.mock.calls.map(([, options]) => options as RequestInit)
  expect(init[0]).toMatchObject({ signal, body: JSON.stringify(search), method: 'POST' })
  expect(init[1].body).toBe(JSON.stringify({ ...search, replacement: '' }))
  expect(init[2].body).toBe(init[3].body)
  expect(init[2].body).toBe(JSON.stringify(body))
  for (const i of [2, 3, 5]) expect(init[i].headers).toMatchObject({
    'X-Editor-Actor-Id': '5', 'X-Editor-Actor-Created-At': actor.createdAt,
  })
})
