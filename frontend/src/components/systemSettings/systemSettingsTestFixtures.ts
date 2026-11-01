import type { SystemSettingBoolItem, SystemSettingBoolKey, SystemSettingIntItem, SystemSettingIntKey, SystemSettings } from '../../api/types'

export const boolItem = (key: SystemSettingBoolKey, value = true, overridden = false): SystemSettingBoolItem => ({ key, value, deployment_value: true, default_value: true, source: overridden ? 'admin' : 'deployment', deployment_source: 'deployment', overridden, requires_restart: false })
export const intItem = (key: SystemSettingIntKey, value: number, overridden = false): SystemSettingIntItem => ({ key, value, deployment_value: key === 'task_max_concurrency' ? 4 : key === 'task_max_per_user' ? 2 : 120, default_value: key === 'task_max_concurrency' ? 4 : key === 'task_max_per_user' ? 2 : 120, source: overridden ? 'admin' : 'default', deployment_source: 'default', overridden, requires_restart: false })

export function settings(revision = 0, registration = true, mcp = true): SystemSettings {
  return { revision, updated_at: revision ? '2026-11-01T03:00:00Z' : null, items: [boolItem('registration_enabled', registration, revision > 0), intItem('task_max_concurrency', 4), intItem('task_max_per_user', 2), boolItem('auto_analyze_enabled'), boolItem('mcp_enabled', mcp), intItem('open_api_rate_limit_per_minute', 120)] }
}

export function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}
