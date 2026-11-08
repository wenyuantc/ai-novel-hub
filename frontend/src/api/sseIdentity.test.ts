import { afterEach, describe, expect, it, vi } from 'vitest'
import { invalidateRequestGeneration, setPermissionDeniedHandler, setUnauthorizedHandler } from './client'
import { isAbortError, runGeneration } from './sse'

const event = (type: string, value: unknown) => `event: ${type}\ndata: ${JSON.stringify(value)}\n\n`

afterEach(() => {
  vi.unstubAllGlobals()
  setPermissionDeniedHandler(null)
  setUnauthorizedHandler(null)
})

describe('SSE 会话代次', () => {
  it.each([401, 403])('旧请求的迟到 %s 不清除或刷新新会话', async (status) => {
    let resolve!: (response: Response) => void
    const response = new Promise<Response>((done) => { resolve = done })
    const unauthorized = vi.fn()
    const denied = vi.fn()
    setUnauthorizedHandler(unauthorized)
    setPermissionDeniedHandler(denied)
    // 模拟已经在网络中、不会因 AbortSignal 立即结束的响应。
    vi.stubGlobal('fetch', vi.fn(() => response))
    const old = runGeneration('/old', {}, { onDelta: () => {} }).catch((error: unknown) => error)
    invalidateRequestGeneration()
    resolve(new Response('{"detail":"无权访问","code":"permission_denied"}', { status }))
    await old
    expect(unauthorized).not.toHaveBeenCalled()
    expect(denied).not.toHaveBeenCalled()
  })

  it.each([401, 403])('当前会话的 %s 仍执行原有全局错误处理', async (status) => {
    const unauthorized = vi.fn()
    const denied = vi.fn()
    setUnauthorizedHandler(unauthorized)
    setPermissionDeniedHandler(denied)
    vi.stubGlobal('fetch', async () => new Response('{"detail":"无权访问","code":"permission_denied"}', { status }))
    await expect(runGeneration('/current', {}, { onDelta: () => {} })).rejects.toMatchObject({ status })
    expect(unauthorized).toHaveBeenCalledTimes(status === 401 ? 1 : 0)
    expect(denied).toHaveBeenCalledTimes(status === 403 ? 1 : 0)
  })

  it('读取中切换会话立即取消连接，同一数据块的后续事件也不能交给新界面', async () => {
    const cancel = vi.fn()
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(event('delta', { text: '旧账号首段' })
          + event('delta', { text: '旧账号后续' }) + event('done', { ok: true })))
      },
      cancel,
    })
    vi.stubGlobal('fetch', async () => new Response(body, { headers: { 'content-type': 'text/event-stream' } }))
    const seen: string[] = []
    const error = await runGeneration('/old', {}, { onDelta: (text) => {
      seen.push(text)
      invalidateRequestGeneration()
    } }).catch((value: unknown) => value)
    expect(isAbortError(error)).toBe(true)
    expect(seen).toEqual(['旧账号首段'])
    expect(cancel).toHaveBeenCalledOnce()
  })

  it('尚无任何数据的旧流也主动中断，随后新会话可正常发起流', async () => {
    const cancel = vi.fn()
    let started!: () => void
    const readerStarted = new Promise<void>((resolve) => { started = resolve })
    const body = new ReadableStream<Uint8Array>({ start: () => started(), cancel })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(body, { headers: { 'content-type': 'text/event-stream' } }))
      .mockResolvedValueOnce(new Response(event('done', { ok: true }), { headers: { 'content-type': 'text/event-stream' } })))
    const old = runGeneration('/old', {}, { onDelta: () => {} }).catch((error: unknown) => error)
    await readerStarted
    // 让请求取得响应体并进入 reader.read()。
    await new Promise((resolve) => setTimeout(resolve, 0))
    invalidateRequestGeneration()
    expect(isAbortError(await old)).toBe(true)
    expect(cancel).toHaveBeenCalledOnce()
    await expect(runGeneration('/new', {}, { onDelta: () => {} })).resolves.toEqual({ ok: true })
  })
})
