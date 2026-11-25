import { request } from './client'
import type { SystemSettings, SystemSettingsUpdate, TaskDrainStatus } from './types'

const base = '/admin/system-settings'

export const systemSettingsApi = {
  get: (signal?: AbortSignal) => request<SystemSettings>(base, { signal }),
  update: (body: SystemSettingsUpdate, signal?: AbortSignal) => request<SystemSettings>(base, { method: 'PATCH', body, signal }),
  drainStatus: (signal: AbortSignal, isCurrent: () => boolean) => request<TaskDrainStatus>(`${base}/drain-status`, { signal, isCurrent }),
}
