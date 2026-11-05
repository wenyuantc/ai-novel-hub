import { afterEach, describe, expect, it, vi } from 'vitest'
import { mentionApi } from './mentions'

afterEach(() => vi.unstubAllGlobals())
describe('提及API合同', () => {
  it('当前草稿只发送预览请求，传递取消信号和Cookie', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ spans: [], content_hash: 'hash', dictionary_hash: 'dict' })))
    vi.stubGlobal('fetch', fetcher)
    const controller = new AbortController()
    await mentionApi.preview(4, '😀林远\n天剑', controller.signal)
    expect(fetcher).toHaveBeenCalledWith('/api/v1/projects/4/mentions/preview', expect.objectContaining({ method: 'POST', credentials: 'include', signal: controller.signal, body: JSON.stringify({ text: '😀林远\n天剑' }) }))
  })
  it('列表、统计和原文窗口保留筛选及指纹，不把码点变成UTF16', async () => {
    const fetcher = vi.fn().mockImplementation(() => Promise.resolve(new Response('{}')))
    vi.stubGlobal('fetch', fetcher)
    await mentionApi.list(2, { entity_kind: 'world', entity_id: 8, source_type: 'knowledge', page: 3 })
    await mentionApi.summary(2, { entity_kind: 'world', category: 'place', entity_id: 8, chapter_from: 2, chapter_to: 4 })
    await mentionApi.source(2, 'chat', 9, { start: 1, end: 3, content_hash: 'a'.repeat(64) })
    const calls = fetcher.mock.calls.map((args) => new URL(String(args[0]), 'https://example.test'))
    expect(calls[0].searchParams.get('source_type')).toBe('knowledge')
    expect(calls[1].pathname).toBe('/api/v1/projects/2/mentions/summary')
    expect(Object.fromEntries(calls[1].searchParams)).toMatchObject({ category: 'place', entity_id: '8', chapter_from: '2', chapter_to: '4' })
    expect(calls[2].pathname).toBe('/api/v1/projects/2/mentions/sources/chat/9')
    expect(Object.fromEntries(calls[2].searchParams)).toEqual({ start: '1', end: '3', content_hash: 'a'.repeat(64) })
  })
})
