import { afterEach, describe, expect, it, vi } from 'vitest'
import { embeddingDimension, modelTypesFor } from './aiConfigs'
import { ApiError } from './client'
import { duplicateDocumentId, kbApi } from './kb'
import { memoryApi } from './memories'

interface Call {
  url: string
  init: RequestInit
}

function stubFetch(status: number, body?: unknown): Call[] {
  const calls: Call[] = []
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    calls.push({ url, init })
    return new Response(status === 204 || body === undefined ? null : JSON.stringify(body), { status })
  })
  return calls
}

afterEach(() => vi.unstubAllGlobals())

describe('kbApi', () => {
  it('上传走 multipart：body 为 FormData（字段 file），不手动设置 Content-Type', async () => {
    const calls = stubFetch(202, { document: { id: 1 }, task: { id: 2 } })
    const file = new File(['# 标题\n正文'], '设定.md', { type: 'text/markdown' })
    const r = await kbApi.upload(3, file)
    expect(r.task.id).toBe(2)
    expect(calls[0].url).toBe('/api/v1/projects/3/kb/documents')
    expect(calls[0].init.method).toBe('POST')
    expect(calls[0].init.credentials).toBe('include')
    const body = calls[0].init.body
    expect(body).toBeInstanceOf(FormData)
    expect(((body as FormData).get('file') as File).name).toBe('设定.md')
    const headers = calls[0].init.headers as Record<string, string>
    expect(headers['Content-Type']).toBeUndefined()
  })

  it('重复上传 409：读出已有文档 id；其他错误为 null', async () => {
    stubFetch(409, { detail: '该文件已上传过：《设定.md》', document_id: 7 })
    const err = await kbApi.upload(3, new File(['x'], '设定.md')).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).detail).toBe('该文件已上传过：《设定.md》')
    expect(duplicateDocumentId(err)).toBe(7)
    expect(duplicateDocumentId(new ApiError(400, 'x', { document_id: 7 }))).toBeNull()
    expect(duplicateDocumentId(new Error('x'))).toBeNull()
  })

  it('其他路径与方法', async () => {
    const calls = stubFetch(200, {})
    await kbApi.getSettings(3)
    await kbApi.updateSettings(3, { top_k: 5 })
    await kbApi.listDocuments(3)
    await kbApi.setInject(3, 4, false)
    await kbApi.reindex(3, 4)
    await kbApi.chunks(3, 4, 2, 20)
    await kbApi.search(3, { query: '天剑宗' })
    await kbApi.indexStatus(3)
    await kbApi.rebuild(3)
    expect(calls.map((c) => [c.init.method, c.url])).toEqual([
      ['GET', '/api/v1/projects/3/kb/settings'],
      ['PATCH', '/api/v1/projects/3/kb/settings'],
      ['GET', '/api/v1/projects/3/kb/documents'],
      ['PATCH', '/api/v1/projects/3/kb/documents/4'],
      ['POST', '/api/v1/projects/3/kb/documents/4/reindex'],
      ['GET', '/api/v1/projects/3/kb/documents/4/chunks?page=2&page_size=20'],
      ['POST', '/api/v1/projects/3/kb/search'],
      ['GET', '/api/v1/projects/3/kb/index-status'],
      ['POST', '/api/v1/projects/3/kb/rebuild'],
    ])
    expect(JSON.parse(calls[3].init.body as string)).toEqual({ inject: false })
    expect((calls[1].init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
  })

  it('删除：204', async () => {
    const calls = stubFetch(204)
    await expect(kbApi.remove(3, 4)).resolves.toBeUndefined()
    expect(calls[0].init.method).toBe('DELETE')
  })
})

describe('memoryApi', () => {
  it('列表查询参数：省略空值，q 去空白', async () => {
    const calls = stubFetch(200, { items: [], total: 0, page: 1, page_size: 50 })
    await memoryApi.list(3, { kind: 'hook', chapter_from: 2, chapter_to: 9, pinned: true, muted: false, q: '  林远 ', page: 2 })
    await memoryApi.list(3, { q: '   ' })
    expect(calls[0].url).toBe(
      '/api/v1/projects/3/memories?kind=hook&chapter_from=2&chapter_to=9&pinned=true&muted=false&q=%E6%9E%97%E8%BF%9C&page=2',
    )
    expect(calls[1].url).toBe('/api/v1/projects/3/memories')
  })

  it('增改删、检索调试与注入预览', async () => {
    const calls = stubFetch(200, {})
    await memoryApi.create(3, { chapter_no: 2, content: '林远拜师' })
    await memoryApi.update(3, 5, { pinned: true })
    await memoryApi.search(3, { query: '拜师', before_chapter_no: 8, include_muted: true })
    await memoryApi.contextPreview(11)
    expect(calls.map((c) => [c.init.method, c.url])).toEqual([
      ['POST', '/api/v1/projects/3/memories'],
      ['PATCH', '/api/v1/projects/3/memories/5'],
      ['POST', '/api/v1/projects/3/memories/search'],
      ['GET', '/api/v1/chapters/11/context-preview'],
    ])
    expect(JSON.parse(calls[2].init.body as string)).toEqual({ query: '拜师', before_chapter_no: 8, include_muted: true })
  })

  it('自动记忆删除 400 原样透传', async () => {
    stubFetch(400, { detail: '自动记忆不能删除，可以屏蔽它' })
    await expect(memoryApi.remove(3, 5)).rejects.toMatchObject({ status: 400, detail: '自动记忆不能删除，可以屏蔽它' })
  })
})

describe('向量模型配置', () => {
  it('Anthropic 不显示向量模型', () => {
    expect(modelTypesFor('anthropic')).toEqual(['text', 'image'])
    expect(modelTypesFor('openai_compat')).toEqual(['text', 'image', 'embedding'])
    expect(modelTypesFor('gemini')).toContain('embedding')
    expect(modelTypesFor(undefined)).toContain('embedding')
  })

  it('从连通性测试结果读出向量维度', () => {
    expect(embeddingDimension('连接成功，向量维度 1536')).toBe(1536)
    expect(embeddingDimension('连接成功')).toBeNull()
  })
})
