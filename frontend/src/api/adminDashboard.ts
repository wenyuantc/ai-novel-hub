import { request } from './client'
import type { AdminDashboardDays, AdminDashboardHistory, AdminDashboardOverview, AdminDashboardTasks } from './types'

const base = '/admin/dashboard'

/** 管理统计的三个独立读取入口，沿用 Cookie、中文错误和可取消请求。 */
export const adminDashboardApi = {
  overview: (signal?: AbortSignal) => request<AdminDashboardOverview>(`${base}/overview`, { signal }),
  tasks: (signal?: AbortSignal) => request<AdminDashboardTasks>(`${base}/tasks`, { signal }),
  history: (days: AdminDashboardDays = 30, signal?: AbortSignal) =>
    request<AdminDashboardHistory>(`${base}/history`, { query: { days }, signal }),
}
