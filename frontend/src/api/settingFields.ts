import { ApiError, request } from './client'
import { storyPositionQuery } from '../utils/storyPosition'
import type {
  SettingEntryType, SettingFieldDefinitionCreate, SettingFieldDefinitionList, SettingFieldDefinitionMutation,
  SettingFieldDefinitionUpdate, SettingFieldOptionCreate, SettingFieldOptionMutation, SettingFieldOptionUpdate,
  SettingFieldsRevision, SettingFieldType,
  SettingEntryPage, SettingEntryRef, SettingEntryFields, SettingFieldValueWrite, SettingFieldProgressionCreate,
  SettingFieldProgressionMutation, SettingLegacyBinding, ProgressionPositionInput,
} from './types'

const definitions = (pid: number) => `/projects/${pid}/setting-field-definitions`
const definition = (pid: number, id: number) => `${definitions(pid)}/${id}`
const option = (pid: number, id: number, optionId: number) => `${definition(pid, id)}/options/${optionId}`
const entryFields = (pid: number, entry: SettingEntryRef) => `/projects/${pid}/setting-entries/${entry.kind}/${entry.id}/fields`

export const settingFieldsApi = {
  listEntries: (pid: number, query: { types?: SettingEntryType[]; q?: string; page?: number; page_size?: number } = {}, signal?: AbortSignal) =>
    request<SettingEntryPage>(`/projects/${pid}/setting-entries`, { query: { ...query }, signal }),
  getEntryFields: (pid: number, entry: SettingEntryRef, position: ProgressionPositionInput | null = null, signal?: AbortSignal) =>
    request<SettingEntryFields>(entryFields(pid, entry), { query: position ? { ...storyPositionQuery(position) } : undefined, signal }),
  updateBaseline: (pid: number, entry: SettingEntryRef, definitionId: number, body: SettingFieldValueWrite, signal?: AbortSignal) =>
    request<SettingEntryFields>(`${entryFields(pid, entry)}/${definitionId}/baseline`, { method: 'PATCH', body, signal }),
  createProgression: (pid: number, entry: SettingEntryRef, definitionId: number, body: SettingFieldProgressionCreate, signal?: AbortSignal) =>
    request<SettingFieldProgressionMutation>(`${entryFields(pid, entry)}/${definitionId}/progressions`, { method: 'POST', body, signal }),
  bindLegacyField: (pid: number, entry: SettingEntryRef, fieldId: number, body: SettingLegacyBinding, signal?: AbortSignal) =>
    request<SettingEntryFields>(`/projects/${pid}/setting-entries/${entry.kind}/${entry.id}/legacy-fields/${fieldId}/binding`, { method: 'POST', body, signal }),
  listDefinitions: (pid: number, query: { types?: SettingEntryType[]; include_archived?: boolean } = {}, signal?: AbortSignal) =>
    request<SettingFieldDefinitionList>(definitions(pid), { query: { ...query }, signal }),
  getDefinition: (pid: number, id: number, signal?: AbortSignal) =>
    request<SettingFieldDefinitionMutation>(definition(pid, id), { signal }),
  createDefinition: (pid: number, body: SettingFieldDefinitionCreate, signal?: AbortSignal) =>
    request<SettingFieldDefinitionMutation>(definitions(pid), { method: 'POST', body, signal }),
  updateDefinition: (pid: number, id: number, body: SettingFieldDefinitionUpdate, signal?: AbortSignal) =>
    request<SettingFieldDefinitionMutation>(definition(pid, id), { method: 'PATCH', body, signal }),
  removeDefinition: (pid: number, id: number, expectedFieldsRevision: number, signal?: AbortSignal) =>
    request<SettingFieldsRevision>(definition(pid, id), { method: 'DELETE', query: { expected_fields_revision: expectedFieldsRevision }, signal }),
  createOption: (pid: number, id: number, body: SettingFieldOptionCreate, signal?: AbortSignal) =>
    request<SettingFieldOptionMutation>(`${definition(pid, id)}/options`, { method: 'POST', body, signal }),
  updateOption: (pid: number, id: number, optionId: number, body: SettingFieldOptionUpdate, signal?: AbortSignal) =>
    request<SettingFieldOptionMutation>(option(pid, id, optionId), { method: 'PATCH', body, signal }),
  removeOption: (pid: number, id: number, optionId: number, expectedFieldsRevision: number, signal?: AbortSignal) =>
    request<SettingFieldsRevision>(option(pid, id, optionId), { method: 'DELETE', query: { expected_fields_revision: expectedFieldsRevision }, signal }),
}

export const SETTING_FIELD_TYPE_LABELS: Record<SettingFieldType, string> = {
  text: '多行文本', line: '单行文本', dropdown: '单选下拉', reference: '条目引用',
}
export const SETTING_ENTRY_TYPE_LABELS: Record<SettingEntryType, string> = {
  character: '角色', 'world.place': '世界观 · 地点', 'world.faction': '世界观 · 势力', 'world.item': '世界观 · 物品',
  'world.rule': '世界观 · 规则', 'world.history': '世界观 · 历史', 'world.other': '世界观 · 其他', organization: '组织', career: '职业',
}
export const SETTING_ENTRY_TYPE_OPTIONS = Object.entries(SETTING_ENTRY_TYPE_LABELS).map(([value, label]) => ({ value, label }))

/** 业务依赖冲突不可当成可自动覆盖的目录版本冲突。 */
export function isSettingFieldsRevisionConflict(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409 && error.body?.code === 'stale_revision' && error.body?.scope === 'fields'
}
