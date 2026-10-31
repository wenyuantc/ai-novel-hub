import { request } from './client'
import type { AdminTaskCancelOut, AdminTaskCancelRequest, AdminTaskDetail, AdminTaskList, AdminTaskListQuery } from './types'

const base = '/admin/tasks'

/** 只读取安全管理投影，不访问个人任务、输出、SSE 或模型配置。 */
export const adminTasksApi = {
  list: ({ q, ...query }: AdminTaskListQuery = {}, signal?: AbortSignal) =>
    request<AdminTaskList>(base, { query: { ...query, q: q?.trim() || undefined }, signal }),
  get: (taskKey: string, signal?: AbortSignal) =>
    request<AdminTaskDetail>(`${base}/${encodeURIComponent(taskKey)}`, { signal }),
  cancel: (taskKey: string, body: AdminTaskCancelRequest, signal?: AbortSignal) =>
    request<AdminTaskCancelOut>(`${base}/${encodeURIComponent(taskKey)}/cancel`, { method: 'POST', body, signal }),
}
