// 基于 fetch + ReadableStream 的 SSE 客户端（局部改写为 POST + JSON body；任务订阅为 GET，同样走 fetch 以统一错误处理与 401）

import { ApiError, buildUrl, handleErrorResponse } from './client'

export interface SSEMessage {
  event: string
  data: string
}

/**
 * 增量 SSE 解析器：可以喂入任意切分的文本块，
 * 处理跨块截断、CRLF / CR / LF 行尾、注释帧（以 ":" 开头）与多行 data。
 */
export class SSEParser {
  private buffer = ''
  private eventName = ''
  private dataLines: string[] = []

  feed(chunk: string): SSEMessage[] {
    this.buffer += chunk
    const out: SSEMessage[] = []
    let start = 0
    const buf = this.buffer
    for (let i = 0; i < buf.length; i++) {
      const ch = buf[i]
      if (ch !== '\n' && ch !== '\r') continue
      if (ch === '\r') {
        // 块末尾的 \r 可能与下一块开头的 \n 组成 CRLF，暂不处理
        if (i === buf.length - 1) break
        this.processLine(buf.slice(start, i), out)
        if (buf[i + 1] === '\n') i++
      } else {
        this.processLine(buf.slice(start, i), out)
      }
      start = i + 1
    }
    this.buffer = buf.slice(start)
    return out
  }

  /** 流结束时调用：处理残留的最后一行与未以空行结尾的事件。 */
  flush(): SSEMessage[] {
    const out: SSEMessage[] = []
    let rest = this.buffer
    this.buffer = ''
    if (rest.endsWith('\r')) rest = rest.slice(0, -1)
    if (rest) this.processLine(rest, out)
    this.dispatch(out)
    return out
  }

  private processLine(line: string, out: SSEMessage[]): void {
    if (line === '') {
      this.dispatch(out)
      return
    }
    if (line.startsWith(':')) return
    const colon = line.indexOf(':')
    let field: string
    let value: string
    if (colon === -1) {
      field = line
      value = ''
    } else {
      field = line.slice(0, colon)
      value = line.slice(colon + 1)
      if (value.startsWith(' ')) value = value.slice(1)
    }
    if (field === 'event') this.eventName = value
    else if (field === 'data') this.dataLines.push(value)
  }

  private dispatch(out: SSEMessage[]): void {
    if (this.dataLines.length > 0) {
      out.push({ event: this.eventName || 'message', data: this.dataLines.join('\n') })
    }
    this.eventName = ''
    this.dataLines = []
  }
}

export interface StreamHandlers {
  /** 返回 false 时立即结束读取并释放连接。 */
  onMessage: (msg: SSEMessage) => void | false
}

/**
 * 打开一个 SSE 响应并逐条回调消息（POST 与 GET 共用）。
 * 推流前的业务错误（400/404/409 等）以 ApiError 抛出；中止时抛出 AbortError。
 */
async function readSSE(
  path: string,
  init: RequestInit,
  handlers: StreamHandlers,
  signal?: AbortSignal,
  onActivity?: () => void,
): Promise<void> {
  let resp: Response
  try {
    resp = await fetch(buildUrl(path), { ...init, credentials: 'include', signal })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err
    throw new ApiError(0, '网络错误，无法连接服务器')
  }
  if (!resp.ok) await handleErrorResponse(path, resp)
  const contentType = resp.headers.get('content-type') ?? ''
  if (!contentType.includes('text/event-stream') || !resp.body) {
    throw new ApiError(resp.status, '服务器未返回流式响应')
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  const parser = new SSEParser()
  const cancelReader = () => { void reader.cancel().catch(() => {}) }
  signal?.addEventListener('abort', cancelReader, { once: true })
  try {
    if (signal?.aborted) throw new DOMException('aborted', 'AbortError')
    for (;;) {
      const { value, done } = await reader.read()
      if (signal?.aborted) throw new DOMException('aborted', 'AbortError')
      if (done) break
      // 注释心跳也算活动，避免模型长时间没有正文时误判连接失活。
      onActivity?.()
      for (const msg of parser.feed(decoder.decode(value, { stream: true }))) {
        if (handlers.onMessage(msg) === false) return
      }
    }
    for (const msg of parser.feed(decoder.decode())) {
      if (handlers.onMessage(msg) === false) return
    }
    for (const msg of parser.flush()) {
      if (handlers.onMessage(msg) === false) return
    }
  } finally {
    signal?.removeEventListener('abort', cancelReader)
    cancelReader()
    reader.releaseLock()
  }
}

/** POST 一个 SSE 接口（局部改写预览）。 */
export function postSSE(path: string, body: unknown, handlers: StreamHandlers, signal?: AbortSignal): Promise<void> {
  return readSSE(
    path,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(body ?? {}),
    },
    handlers,
    signal,
  )
}

// 后端每 15 秒发送一次心跳，允许连续缺失三次后重连。
const STREAM_IDLE_TIMEOUT_MS = 45_000

/** GET 任务订阅：响应头或正文持续无数据时断开，由 subscribeTask 按原退避策略重连。 */
export async function getSSE(path: string, handlers: StreamHandlers, signal?: AbortSignal): Promise<void> {
  const controller = new AbortController()
  let timer: ReturnType<typeof setTimeout> | undefined
  const interrupted = new Promise<never>((_, reject) => {
    controller.signal.addEventListener('abort', () => reject(controller.signal.reason), { once: true })
  })
  const abort = () => controller.abort()
  const resetTimeout = () => {
    clearTimeout(timer)
    timer = setTimeout(() => controller.abort(new StreamError('流式连接长时间无响应')), STREAM_IDLE_TIMEOUT_MS)
  }
  signal?.addEventListener('abort', abort, { once: true })
  try {
    if (signal?.aborted) abort()
    else resetTimeout()
    await Promise.race([
      readSSE(path, { method: 'GET', headers: { Accept: 'text/event-stream' } }, handlers, controller.signal, resetTimeout),
      interrupted,
    ])
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', abort)
    controller.abort()
  }
}

/** 业务流事件：delta 增量文本 / done 完成载荷 / error 流内错误；其余事件（如 continue）交给 onEvent。 */
export interface GenerationCallbacks<TDone> {
  onDelta: (text: string) => void
  signal?: AbortSignal
  /** 可选：接收 delta/done/error 以外的事件（data 已按 JSON 解析） */
  onEvent?: (event: string, data: unknown) => void
  /** 可选：校验 done 载荷 */
  parseDone?: (data: unknown) => TDone
}

export class StreamError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'StreamError'
  }
}

export function readDetail(data: unknown): string {
  if (typeof data === 'object' && data !== null && 'detail' in data) {
    const detail = (data as { detail: unknown }).detail
    if (typeof detail === 'string') return detail
  }
  return '生成失败'
}

export function readText(data: unknown): string {
  if (typeof data === 'object' && data !== null && 'text' in data) {
    const text = (data as { text: unknown }).text
    if (typeof text === 'string') return text
  }
  return ''
}

/**
 * 运行一次生成流：增量文本回调 onDelta，完成时 resolve done 载荷；
 * 收到 error 事件抛 StreamError；流意外结束（无 done）也抛错。
 */
export async function runGeneration<TDone>(path: string, body: unknown, cb: GenerationCallbacks<TDone>): Promise<TDone> {
  let result: { value: TDone } | null = null
  let streamError: string | null = null
  await postSSE(
    path,
    body,
    {
      onMessage: (msg) => {
        let data: unknown
        try {
          data = JSON.parse(msg.data)
        } catch {
          return
        }
        if (msg.event === 'delta') {
          const text = readText(data)
          if (text) cb.onDelta(text)
        } else if (msg.event === 'done') {
          result = { value: cb.parseDone ? cb.parseDone(data) : (data as TDone) }
        } else if (msg.event === 'error') {
          streamError = readDetail(data)
        } else {
          cb.onEvent?.(msg.event, data)
        }
      },
    },
    cb.signal,
  )
  if (streamError !== null) throw new StreamError(streamError)
  const final = result as { value: TDone } | null
  if (final === null) throw new StreamError('连接意外中断，生成未完成')
  return final.value
}

export function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError'
}
