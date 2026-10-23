// 统一的 fetch 封装：Cookie 会话、JSON 编解码、错误归一化

export const API_BASE = '/api/v1'

export class ApiError extends Error {
  readonly status: number
  readonly detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

type UnauthorizedHandler = () => void
let unauthorizedHandler: UnauthorizedHandler | null = null

/** 注册 401 处理器（由 auth store 注入，用于清空会话并跳转登录页）。 */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler
}

export function notifyUnauthorized(): void {
  unauthorizedHandler?.()
}

export type QueryValue = string | number | boolean | null | undefined | Array<string | number>

export function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const params = new URLSearchParams()
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === null || value === undefined) continue
      if (Array.isArray(value)) {
        for (const v of value) params.append(key, String(v))
      } else {
        params.append(key, String(value))
      }
    }
  }
  const qs = params.toString()
  return `${API_BASE}${path}${qs ? `?${qs}` : ''}`
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

/** 从错误响应体中提取 detail（后端统一为 {"detail": "中文"}）。 */
export async function readErrorDetail(resp: Response): Promise<string> {
  const fallback = `请求失败（HTTP ${resp.status}）`
  try {
    const text = await resp.text()
    if (!text) return fallback
    const data: unknown = JSON.parse(text)
    if (isRecord(data) && typeof data.detail === 'string') return data.detail
    return fallback
  } catch {
    return fallback
  }
}

/** 判断某路径的 401 是否应触发全局登出跳转（登录类接口自身的 401 由页面处理）。 */
function shouldRedirectOn401(path: string): boolean {
  return !path.startsWith('/auth/login') && !path.startsWith('/auth/me')
}

export async function handleErrorResponse(path: string, resp: Response): Promise<never> {
  const detail = await readErrorDetail(resp)
  if (resp.status === 401 && shouldRedirectOn401(path)) notifyUnauthorized()
  throw new ApiError(resp.status, detail)
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'
  body?: unknown
  query?: Record<string, QueryValue>
  signal?: AbortSignal
}

export async function rawRequest(path: string, options: RequestOptions = {}): Promise<Response> {
  const { method = 'GET', body, query, signal } = options
  const headers: Record<string, string> = { Accept: 'application/json' }
  let payload: string | undefined
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }
  let resp: Response
  try {
    resp = await fetch(buildUrl(path, query), { method, headers, body: payload, signal, credentials: 'include' })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err
    throw new ApiError(0, '网络错误，无法连接服务器')
  }
  if (!resp.ok) await handleErrorResponse(path, resp)
  return resp
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const resp = await rawRequest(path, options)
  if (resp.status === 204) return undefined as T
  const text = await resp.text()
  return (text ? JSON.parse(text) : undefined) as T
}

export const api = {
  get: <T>(path: string, query?: Record<string, QueryValue>) => request<T>(path, { query }),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body }),
  patch: <T>(path: string, body: unknown) => request<T>(path, { method: 'PATCH', body }),
  del: (path: string) => request<void>(path, { method: 'DELETE' }),
}

/** 把任意错误转成适合展示的中文消息。 */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail
  if (err instanceof Error) return err.message
  return '未知错误'
}
