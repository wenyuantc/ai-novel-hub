// 后台任务：CRUD + 订阅（GET /tasks/{id}/stream，断线自动重连，snapshot 覆盖本地文本）

import { ApiError, api, request, type QueryValue } from './client'
import { getSSE, isAbortError, readDetail, readText, StreamError } from './sse'
import type {
  ContinueEvent,
  Task,
  TaskClearResult,
  TaskKind,
  TaskListQuery,
  TaskOutput,
  TaskProgress,
  TaskStatus,
  WizardKind,
} from './types'

export const taskApi = {
  list: (query: TaskListQuery = {}) => api.get<Task[]>('/tasks', { ...query } as Record<string, QueryValue>),
  get: (id: number) => api.get<Task>(`/tasks/${id}`),
  output: (id: number) => api.get<TaskOutput>(`/tasks/${id}/output`),
  cancel: (id: number) => api.post<Task>(`/tasks/${id}/cancel`),
  retry: (id: number) => api.post<Task>(`/tasks/${id}/retry`),
  remove: (id: number) => api.del(`/tasks/${id}`),
  /** 清理已结束任务（DELETE 带查询参数，api.del 不支持 query） */
  clearFinished: (projectId?: number) =>
    request<TaskClearResult>('/tasks', { method: 'DELETE', query: { finished: true, project_id: projectId } }),
}

export function isActiveTask(status: TaskStatus): boolean {
  return status === 'queued' || status === 'running'
}

/** 只有失败、已取消、已中断的任务可以重试（与后端一致） */
export function canRetryTask(status: TaskStatus): boolean {
  return status === 'failed' || status === 'cancelled' || status === 'interrupted'
}

export const TASK_STATUS: Record<TaskStatus, { label: string; color: string }> = {
  queued: { label: '排队中', color: 'default' },
  running: { label: '运行中', color: 'processing' },
  succeeded: { label: '已完成', color: 'success' },
  failed: { label: '失败', color: 'error' },
  cancelled: { label: '已取消', color: 'warning' },
  interrupted: { label: '已中断', color: 'magenta' },
}

export const TASK_KIND_LABELS: Record<TaskKind, string> = {
  'chapter.generate': 'AI 生成',
  'chapter.polish': 'AI 润色',
  'wizard.outline': '大纲向导',
  'wizard.characters': '角色向导',
  'wizard.world': '世界观向导',
  'character.generate': 'AI 生成角色',
  'organization.generate': 'AI 生成组织',
  'relations.parse': '解析旧关系',
  'career.generate': 'AI 生成职业',
  'wizard.careers': '职业体系向导',
}

/** 「排队中，前面还有 N 个」 */
export function queueLabel(task: Pick<Task, 'status' | 'queue_position'>): string {
  if (task.status !== 'queued') return ''
  if (task.queue_position === null || task.queue_position === undefined) return '排队中'
  return task.queue_position === 0 ? '排队中，下一个执行' : `排队中，前面还有 ${task.queue_position} 个任务`
}

/**
 * 任务面板「跳转」目标：章节任务 → 章节编辑器；向导任务 → 向导页对应步骤；
 * 角色生成 / 旧关系解析 → 角色页；组织生成 → 组织页；职业生成 → 职业页。无法定位时为 null。
 */
export function taskLink(task: Pick<Task, 'kind' | 'project_id' | 'chapter_id'>): string | null {
  if (task.project_id === null) return null
  const base = `/projects/${task.project_id}`
  switch (task.kind) {
    case 'chapter.generate':
    case 'chapter.polish':
      return task.chapter_id === null ? null : `${base}/chapters/${task.chapter_id}`
    case 'character.generate':
    case 'relations.parse':
      return `${base}/characters`
    case 'organization.generate':
      return `${base}/organizations`
    case 'career.generate':
      return `${base}/careers`
    default:
      return `${base}/wizard?kind=${task.kind.slice('wizard.'.length)}`
  }
}

/** 向导类型 → 任务 kind */
export function wizardTaskKind(kind: WizardKind): TaskKind {
  return `wizard.${kind}`
}

// ---------------- 订阅 ----------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function readTask(data: unknown): Task | null {
  if (!isRecord(data) || !isRecord(data.task)) return null
  const t = data.task
  return typeof t.id === 'number' && typeof t.status === 'string' ? (t as unknown as Task) : null
}

export function isContinueEvent(data: unknown): data is ContinueEvent {
  if (!isRecord(data)) return false
  return typeof data.round === 'number' && typeof data.words === 'number' && typeof data.target === 'number'
}

function isProgress(data: unknown): data is TaskProgress {
  return isRecord(data) && typeof data.status === 'string' && typeof data.progress === 'number'
}

/** 订阅终止结果：成功带 result（章节 {chapter, warning?}；向导 {kind, items}），否则带中文 detail */
export type TaskEnd =
  | { status: 'succeeded'; task: Task | null; result: unknown }
  | { status: 'failed' | 'cancelled' | 'interrupted'; task: Task | null; detail: string }

export interface TaskStreamHandlers {
  /** 每次（重）连接后的第一帧：用 text 覆盖本地已累积文本；task 极少数情况下为 null */
  onSnapshot?: (task: Task | null, text: string) => void
  onDelta?: (text: string) => void
  onContinue?: (e: ContinueEvent) => void
  onProgress?: (p: TaskProgress) => void
  /** 连接断开、即将第 attempt 次重连 */
  onReconnect?: (attempt: number, delayMs: number) => void
}

export interface SubscribeOptions {
  /** 连续失败的最大重连次数，默认 8（收到 snapshot 且连接存活满 stableMs 才清零） */
  maxRetries?: number
  baseDelayMs?: number
  maxDelayMs?: number
  /** 连接收到 snapshot 后至少存活多久才算「稳定」并清零失败计数，默认 5000 ms；防止「连上即断」时无限 ~1s 重连 */
  stableMs?: number
  /** 测试注入 */
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>
  now?: () => number
}

/** 第 attempt（从 1 起）次重连前的等待：指数退避，封顶 maxDelayMs */
export function reconnectDelay(attempt: number, baseDelayMs = 1000, maxDelayMs = 15000): number {
  return Math.min(baseDelayMs * 2 ** Math.max(0, attempt - 1), maxDelayMs)
}

function abortableSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('aborted', 'AbortError'))
      return
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    const onAbort = () => {
      clearTimeout(timer)
      reject(new DOMException('aborted', 'AbortError'))
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

/** 网络错误、5xx、非流式响应、流意外结束可以重连；4xx（401/404 等）不重连 */
function isRetryable(err: unknown): boolean {
  if (err instanceof ApiError) return err.status === 0 || err.status >= 500 || err.status < 400
  return err instanceof StreamError || err instanceof TypeError
}

/**
 * 订阅任务直到结束（done / error）。断线按指数退避自动重连，重连后的 snapshot 覆盖本地文本。
 * 中止（signal）抛 AbortError，只断开订阅，不影响任务；4xx 抛 ApiError；重连耗尽抛最后一次错误。
 */
export async function subscribeTask(
  taskId: number,
  handlers: TaskStreamHandlers,
  signal?: AbortSignal,
  options: SubscribeOptions = {},
): Promise<TaskEnd> {
  const {
    maxRetries = 8,
    baseDelayMs = 1000,
    maxDelayMs = 15000,
    stableMs = 5000,
    sleep = abortableSleep,
    now = Date.now,
  } = options
  let failures = 0
  for (;;) {
    const holder: { end: TaskEnd | null } = { end: null }
    // 本次连接收到 snapshot 的时间；断开时据此判断连接是否稳定
    let snapshotAt: number | null = null
    try {
      await getSSE(
        `/tasks/${taskId}/stream`,
        {
          onMessage: (msg) => {
            if (holder.end) return
            let data: unknown
            try {
              data = JSON.parse(msg.data)
            } catch {
              return
            }
            switch (msg.event) {
              case 'snapshot':
                snapshotAt = now()
                handlers.onSnapshot?.(readTask(data), readText(data))
                break
              case 'delta': {
                const text = readText(data)
                if (text) handlers.onDelta?.(text)
                break
              }
              case 'continue':
                if (isContinueEvent(data)) handlers.onContinue?.(data)
                break
              case 'progress':
                if (isProgress(data)) handlers.onProgress?.(data)
                break
              case 'done':
                holder.end = { status: 'succeeded', task: readTask(data), result: isRecord(data) ? data.result : null }
                break
              case 'error': {
                const task = readTask(data)
                const st = task?.status
                const status = st === 'cancelled' || st === 'interrupted' ? st : 'failed'
                holder.end = { status, task, detail: readDetail(data) }
                break
              }
            }
          },
        },
        signal,
      )
      if (holder.end) return holder.end
      throw new StreamError('连接意外中断')
    } catch (err) {
      if (isAbortError(err) || signal?.aborted) throw err
      if (!isRetryable(err)) throw err
      // 稳定连接（snapshot 后存活满 stableMs）之后的断开视为新一轮故障，计数清零；否则继续累计并退避
      if (snapshotAt !== null && now() - snapshotAt >= stableMs) failures = 0
      if (failures >= maxRetries) throw err
      failures += 1
      const delay = reconnectDelay(failures, baseDelayMs, maxDelayMs)
      handlers.onReconnect?.(failures, delay)
      await sleep(delay, signal)
    }
  }
}
