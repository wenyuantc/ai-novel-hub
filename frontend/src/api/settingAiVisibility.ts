import { ApiError, request } from './client'
import type { SettingEntry, SettingEntryFields, SettingEntryRef } from './types'

export type SettingAiMode = 'always' | 'detected' | 'manual_only' | 'never'

export interface SettingAiSettings {
  ai_mode: SettingAiMode | null
  mention_enabled: boolean
  mention_case_sensitive: boolean
  mention_exclusions: string[]
  ai_hidden_builtin_fields: string[]
  hideable_builtin_fields: string[]
  links: SettingEntry[]
  ai_visibility_revision: number
}

export interface SettingAiSettingsPatch {
  expected_ai_visibility_revision: number
  ai_mode?: SettingAiMode
  mention_enabled?: boolean
  mention_case_sensitive?: boolean
  mention_exclusions?: string[]
  ai_hidden_builtin_fields?: string[]
  links?: SettingEntryRef[]
}

export interface LegacyFieldVisibilityPatch {
  expected_timeline_revision: number
  expected_ai_visibility_revision: number
  ai_hidden: boolean
}

const entryPath = (pid: number, entry: SettingEntryRef) => `/projects/${pid}/setting-entries/${entry.kind}/${entry.id}`

export const settingAiVisibilityApi = {
  get: (pid: number, entry: SettingEntryRef, signal?: AbortSignal) =>
    request<SettingAiSettings>(`${entryPath(pid, entry)}/ai-settings`, { signal }),
  update: (pid: number, entry: SettingEntryRef, body: SettingAiSettingsPatch, signal?: AbortSignal) =>
    request<SettingAiSettings>(`${entryPath(pid, entry)}/ai-settings`, { method: 'PATCH', body, signal }),
  updateLegacyField: (pid: number, entry: SettingEntryRef, fieldId: number, body: LegacyFieldVisibilityPatch, signal?: AbortSignal) =>
    request<SettingEntryFields>(`${entryPath(pid, entry)}/legacy-fields/${fieldId}/ai-visibility`, { method: 'PATCH', body, signal }),
}

/** 版本冲突、断网与服务端故障不可猜测结果；需要读取最新设置再继续。 */
export function aiSettingsNeedsReview(error: unknown): boolean {
  return !(error instanceof ApiError) || error.status === 0 || error.status >= 500
    || (error.status === 409 && error.body?.code === 'stale_revision')
}
