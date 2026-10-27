import { afterEach, describe, expect, it, vi } from 'vitest'
import { withStaleRefresh } from '../utils/bookImport'
import { bookImportApi } from './bookImport'

interface Call {
  method: string
  url: string
  body: RequestInit['body']
}

/** 依次返回 responses（status, body），记录请求 */
function stubFetch(responses: [number, unknown][]): Call[] {
  const calls: Call[] = []
  let i = 0
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    calls.push({ method: init.method ?? 'GET', url, body: init.body })
    const [status, body] = responses[Math.min(i++, responses.length - 1)]
    return new Response(status === 204 || body === undefined ? null : JSON.stringify(body), { status })
  })
  return calls
}

afterEach(() => vi.unstubAllGlobals())

describe('bookImportApi', () => {
  it('上传走 multipart（字段 file），不手动设置 Content-Type', async () => {
    const calls = stubFetch([[201, { id: 3, chapter_count: 12 }]])
    const r = await bookImportApi.upload(new File(['第一章\n正文'], '书.txt'))
    expect(r.id).toBe(3)
    expect(calls[0].method).toBe('POST')
    expect(calls[0].url).toBe('/api/v1/book-imports')
    expect(calls[0].body).toBeInstanceOf(FormData)
    expect(((calls[0].body as FormData).get('file') as File).name).toBe('书.txt')
  })

  it('路径、方法与请求体', async () => {
    const calls = stubFetch([[200, {}]])
    await bookImportApi.list()
    await bookImportApi.get(3)
    await bookImportApi.chapterText(3, 5)
    await bookImportApi.resplit(3, { mode: 'regex', pattern: '^第\\d+章', revision: 2 })
    await bookImportApi.op(3, { op: 'split', index: 5, line: 8, revision: 2 })
    await bookImportApi.estimate(3, { import_range: [1, 10], reverse_range: [2, 5], extract_style: true, deep_analyze: false })
    await bookImportApi.start(3, { revision: 2, title: '书', import_range: [1, 10], reverse_range: [1, 10] })
    await bookImportApi.remove(3)
    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual([
      'GET /api/v1/book-imports',
      'GET /api/v1/book-imports/3',
      'GET /api/v1/book-imports/3/chapters/5',
      'POST /api/v1/book-imports/3/resplit',
      'POST /api/v1/book-imports/3/ops',
      'POST /api/v1/book-imports/3/estimate',
      'POST /api/v1/book-imports/3/start',
      'DELETE /api/v1/book-imports/3',
    ])
    expect(JSON.parse(calls[4].body as string)).toEqual({ op: 'split', index: 5, line: 8, revision: 2 })
    expect(JSON.parse(calls[5].body as string).import_range).toEqual([1, 10])
  })

  it('校正时修订号过期（409）→ 重新拉取详情，拿到最新修订号', async () => {
    const calls = stubFetch([
      [409, { detail: '解析结果已被修改，请刷新后重试', code: 'stale_revision' }],
      [200, { id: 3, revision: 5, chapters: [] }],
    ])
    const outcome = await withStaleRefresh(
      () => bookImportApi.op(3, { op: 'merge_next', index: 1, revision: 4 }),
      () => bookImportApi.get(3),
    )
    expect(outcome.ok).toBe(false)
    expect(outcome.ok ? null : outcome.record.revision).toBe(5)
    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual(['POST /api/v1/book-imports/3/ops', 'GET /api/v1/book-imports/3'])
  })

  it('不带 stale_revision 标记的 409（如已有拆书任务）原样抛出，不重新拉取', async () => {
    const calls = stubFetch([[409, { detail: '已有进行中或待处理的拆书任务，请等待其完成或结束后再试' }]])
    await expect(
      withStaleRefresh(
        () => bookImportApi.op(3, { op: 'merge_next', index: 1, revision: 4 }),
        () => bookImportApi.get(3),
      ),
    ).rejects.toMatchObject({ status: 409, detail: '已有进行中或待处理的拆书任务，请等待其完成或结束后再试' })
    expect(calls).toHaveLength(1)
  })
})
