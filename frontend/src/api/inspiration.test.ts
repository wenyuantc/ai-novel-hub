import { afterEach, describe, expect, it, vi } from 'vitest'
import { generateOptions, inspirationApi, parseGenerateDone, parseRetry, retryText } from './inspiration'
import { StreamError } from './sse'

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

function stubSSE(frames: string): Call[] {
  const calls: Call[] = []
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    calls.push({ url, init })
    return new Response(frames, { status: 200, headers: { 'content-type': 'text/event-stream' } })
  })
  return calls
}

const frame = (event: string, data: unknown) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`

afterEach(() => vi.unstubAllGlobals())

describe('inspirationApi', () => {
  it('路径与方法', async () => {
    const calls = stubFetch(200, {})
    await inspirationApi.list()
    await inspirationApi.create('想法')
    await inspirationApi.fromProject(5)
    await inspirationApi.update(1, { current: 'title' })
    await inspirationApi.updateStep(1, 'tags', { action: 'decide', value: ['A'], source: 'ai' })
    await inspirationApi.addNote(1, '补充')
    await inspirationApi.deleteNote(1, 0)
    await inspirationApi.scaleSuggest(300000)
    await inspirationApi.convert(1, { scale: null })
    await inspirationApi.applyPreview(1, 5)
    await inspirationApi.apply(1, { project_id: 5, fields: ['title'] })
    expect(calls.map((c) => [c.init.method, c.url])).toEqual([
      ['GET', '/api/v1/inspiration/sessions'],
      ['POST', '/api/v1/inspiration/sessions'],
      ['POST', '/api/v1/inspiration/sessions/from-project/5'],
      ['PATCH', '/api/v1/inspiration/sessions/1'],
      ['PATCH', '/api/v1/inspiration/sessions/1/steps/tags'],
      ['POST', '/api/v1/inspiration/sessions/1/notes'],
      ['DELETE', '/api/v1/inspiration/sessions/1/notes/0'],
      ['GET', '/api/v1/inspiration/scale-suggest?target_words=300000'],
      ['POST', '/api/v1/inspiration/sessions/1/convert'],
      ['GET', '/api/v1/inspiration/sessions/1/apply-preview?project_id=5'],
      ['POST', '/api/v1/inspiration/sessions/1/apply'],
    ])
    expect(JSON.parse(String(calls[4].init.body))).toEqual({ action: 'decide', value: ['A'], source: 'ai' })
  })
})

describe('生成选项（同步 SSE）', () => {
  const session = { id: 1, title: 't', status: 'active', state: {} }
  const round = { options: ['A', 'B', 'C', 'D'], feedback: '短一点', created_at: '2026-10-26T00:00:00+00:00' }

  it('retry 事件回调后继续 delta，done 收窄为 {round, session}', async () => {
    const calls = stubSSE(
      ': stream-start\n\n' +
        frame('delta', { text: '坏' }) +
        frame('retry', { attempt: 1, max: 2, reason: '无法解析' }) +
        frame('delta', { text: '{"options"' }) +
        frame('done', { round, session }),
    )
    const events: string[] = []
    const done = await generateOptions(1, 'title', { feedback: '短一点' }, {
      onDelta: (t) => events.push(`delta:${t}`),
      onRetry: (ev) => events.push(`retry:${ev.attempt}/${ev.max}`),
    })
    expect(events).toEqual(['delta:坏', 'retry:1/2', 'delta:{"options"'])
    expect(done.round).toEqual(round)
    expect(done.session).toMatchObject({ id: 1 })
    expect(calls[0].url).toBe('/api/v1/inspiration/sessions/1/steps/title/generate')
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ feedback: '短一点' })
  })

  it('error 事件抛 StreamError', async () => {
    stubSSE(frame('retry', { attempt: 1, max: 2, reason: 'x' }) + frame('error', { detail: 'AI 输出不合法：x，已自动重试 2 次，请稍后重试' }))
    const err = await generateOptions(1, 'title', {}, { onDelta: () => {}, onRetry: () => {} }).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(StreamError)
    expect((err as Error).message).toContain('已自动重试 2 次')
  })

  it('载荷收窄与文案', () => {
    expect(() => parseGenerateDone({})).toThrow()
    expect(parseGenerateDone({ round: { options: ['a', 1] }, session }).round).toEqual({ options: ['a'], feedback: '', created_at: '' })
    expect(parseRetry({ attempt: 2, max: 2, reason: 'r' })).toEqual({ attempt: 2, max: 2, reason: 'r' })
    expect(parseRetry(null)).toEqual({ attempt: 1, max: 2, reason: '' })
    expect(retryText({ attempt: 2, max: 2, reason: '' })).toBe('输出格式不对，正在重试（第 2 次）')
    expect(retryText({ attempt: 1, max: 2, reason: '' })).toBe('输出格式不对，正在重试（第 1 次）')
  })
})
