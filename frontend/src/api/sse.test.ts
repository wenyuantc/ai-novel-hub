import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './client'
import { isAbortError, runGeneration, SSEParser, StreamError, type SSEMessage } from './sse'

function feedAll(chunks: string[]): SSEMessage[] {
  const parser = new SSEParser()
  const out: SSEMessage[] = []
  for (const c of chunks) out.push(...parser.feed(c))
  out.push(...parser.flush())
  return out
}

describe('SSEParser', () => {
  it('解析标准帧并忽略注释帧', () => {
    const msgs = feedAll([': stream-start\n\nevent: delta\ndata: {"text":"你好"}\n\nevent: done\ndata: {}\n\n'])
    expect(msgs).toEqual([
      { event: 'delta', data: '{"text":"你好"}' },
      { event: 'done', data: '{}' },
    ])
  })

  it('处理跨 chunk 截断', () => {
    const full = 'event: delta\ndata: {"text":"第一章"}\n\nevent: delta\ndata: {"text":"续"}\n\n'
    const chunks: string[] = []
    for (let i = 0; i < full.length; i += 3) chunks.push(full.slice(i, i + 3))
    expect(feedAll(chunks)).toEqual([
      { event: 'delta', data: '{"text":"第一章"}' },
      { event: 'delta', data: '{"text":"续"}' },
    ])
  })

  it('处理 CRLF 且 \\r 与 \\n 被拆在两个 chunk', () => {
    const msgs = feedAll([': c\r\n\r', '\nevent: error\r', '\ndata: {"detail":"x"}\r\n\r', '\n'])
    expect(msgs).toEqual([{ event: 'error', data: '{"detail":"x"}' }])
  })

  it('多行 data 以换行拼接，缺省事件名为 message', () => {
    expect(feedAll(['data: a\ndata: b\n\n'])).toEqual([{ event: 'message', data: 'a\nb' }])
  })

  it('流结束时 flush 未以空行结尾的事件', () => {
    expect(feedAll(['event: done\ndata: {"ok":true}'])).toEqual([{ event: 'done', data: '{"ok":true}' }])
  })
})

function sseResponse(frames: string[]): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const f of frames) controller.enqueue(encoder.encode(f))
      controller.close()
    },
  })
  return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } })
}

function frame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
}

describe('runGeneration', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('delta 累积、continue 交给 onEvent、返回 done 载荷', async () => {
    const fetchMock = vi.fn(async () =>
      sseResponse([
        ': stream-start\n\n',
        frame('delta', { text: '首轮' }),
        frame('continue', { round: 1, words: 2, target: 3000 }),
        frame('delta', { text: '\n\n' }),
        frame('delta', { text: '续写' }),
        frame('done', { chapter: { id: 1 }, warning: '自动续写失败：x' }),
      ]),
    )
    vi.stubGlobal('fetch', fetchMock)
    const deltas: string[] = []
    const events: Array<[string, unknown]> = []
    const done = await runGeneration<{ warning?: string }>('/chapters/1/generate', { auto_continue: true }, {
      onDelta: (t) => deltas.push(t),
      onEvent: (e, d) => events.push([e, d]),
    })
    expect(deltas.join('')).toBe('首轮\n\n续写')
    expect(events).toEqual([['continue', { round: 1, words: 2, target: 3000 }]])
    expect(done.warning).toBe('自动续写失败：x')
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/v1/chapters/1/generate')
    expect(init.credentials).toBe('include')
    expect(JSON.parse(String(init.body))).toEqual({ auto_continue: true })
  })

  it('error 事件抛 StreamError；缺少 done 也抛 StreamError', async () => {
    vi.stubGlobal('fetch', async () => sseResponse([frame('delta', { text: 'a' }), frame('error', { detail: '额度不足' })]))
    await expect(runGeneration('/x', {}, { onDelta: () => {} })).rejects.toEqual(new StreamError('额度不足'))
    vi.stubGlobal('fetch', async () => sseResponse([frame('delta', { text: 'a' })]))
    await expect(runGeneration('/x', {}, { onDelta: () => {} })).rejects.toThrow('连接意外中断，生成未完成')
  })

  it('非 2xx 抛带 status/detail 的 ApiError', async () => {
    vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ detail: '正文已变化' }), { status: 409 }))
    const err = await runGeneration('/x', {}, { onDelta: () => {} }).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(409)
    expect((err as ApiError).detail).toBe('正文已变化')
  })

  it('已中止的 signal 抛 AbortError', async () => {
    vi.stubGlobal('fetch', async (_url: string, init: RequestInit) => {
      if (init.signal?.aborted) throw new DOMException('aborted', 'AbortError')
      return sseResponse([])
    })
    const controller = new AbortController()
    controller.abort()
    const err = await runGeneration('/x', {}, { onDelta: () => {}, signal: controller.signal }).catch((e: unknown) => e)
    expect(isAbortError(err)).toBe(true)
  })
})
