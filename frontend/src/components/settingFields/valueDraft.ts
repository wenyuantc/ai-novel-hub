import type { ProgressionSubject, SettingEntryRef, SettingFieldSlot, SettingFieldType, SettingFieldValue } from '../../api/types'
import { fieldValueError } from '../settings/settingFieldDraft'

const positiveId = (value: unknown): value is number => typeof value === 'number' && Number.isInteger(value) && value > 0 && value <= 2_147_483_647
const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)

/** 事件旧 DTO 的 JSON 在类型边界收窄，内部句柄和其它实体不能进入引用控件。 */
export function fieldValueFromRaw(type: SettingFieldType, value: unknown): SettingFieldValue | null {
  if (type === 'text' || type === 'line') return typeof value === 'string' ? { field_type: type, value } : null
  if (type === 'dropdown') return value === null ? { field_type: type, value: null }
    : record(value) && Object.keys(value).length === 1 && positiveId(value.option_id) ? { field_type: type, value: { option_id: value.option_id } } : null
  if (value === null) return { field_type: 'reference', value: null }
  if (!record(value) || Object.keys(value).length !== 2 || !positiveId(value.id)) return null
  const kind = value.kind
  return kind === 'character' || kind === 'world' || kind === 'organization' || kind === 'career'
    ? { field_type: 'reference', value: { kind, id: value.id } } : null
}

export function emptyFieldValue(type: SettingFieldType): SettingFieldValue {
  return type === 'text' || type === 'line' ? { field_type: type, value: '' }
    : type === 'dropdown' ? { field_type: type, value: null } : { field_type: type, value: null }
}

export function fieldSlotValue(slot: SettingFieldSlot, baseline = false): SettingFieldValue {
  switch (slot.field_type) {
    case 'text': case 'line': return { field_type: slot.field_type, value: baseline ? slot.base_value : slot.value }
    case 'dropdown': return { field_type: slot.field_type, value: baseline ? slot.base_value : slot.value }
    case 'reference': return { field_type: slot.field_type, value: baseline ? slot.base_value : slot.value }
  }
}

export function sameFieldValue(a: SettingFieldValue, b: SettingFieldValue): boolean {
  if (a.field_type !== b.field_type) return false
  if (a.value === null || b.value === null) return a.value === b.value
  if (typeof a.value === 'string' || typeof b.value === 'string') return a.value === b.value
  if (a.field_type === 'dropdown' && b.field_type === 'dropdown') return a.value.option_id === b.value.option_id
  return a.field_type === 'reference' && b.field_type === 'reference' && a.value.kind === b.value.kind && a.value.id === b.value.id
}

export function validateEntryFieldValue(draft: SettingFieldValue, slot: SettingFieldSlot, original = fieldSlotValue(slot, true)): string | null {
  if (slot.field_type !== draft.field_type) return '字段类型已变化，请保留草稿后重新核对字段配置'
  const error = fieldValueError(draft)
  if (error) return error
  if (sameFieldValue(draft, original)) return null
  if (slot.archived) return '此字段已停用，不能填写新值'
  if (draft.field_type === 'dropdown' && draft.value !== null) {
    const id = draft.value.option_id
    if (!slot.options.some((option) => option.id === id && !option.archived)) return '请选择当前字段的可用选项'
  }
  return null
}

export function entryProgressionTarget(entry: SettingEntryRef): Extract<ProgressionSubject, { subject_kind: 'character' | 'world' | 'organization' | 'career_entry' }> {
  switch (entry.kind) {
    case 'character': return { subject_kind: 'character', character_id: entry.id }
    case 'world': return { subject_kind: 'world', world_setting_id: entry.id }
    case 'organization': return { subject_kind: 'organization', organization_id: entry.id }
    case 'career': return { subject_kind: 'career_entry', career_id: entry.id }
  }
}

export const entryKey = (entry: SettingEntryRef) => `${entry.kind}:${entry.id}`

export function subjectEntry(subject: ProgressionSubject): SettingEntryRef | null {
  switch (subject.subject_kind) {
    case 'character': return { kind: 'character', id: subject.character_id }
    case 'world': return { kind: 'world', id: subject.world_setting_id }
    case 'organization': return { kind: 'organization', id: subject.organization_id }
    case 'career_entry': return { kind: 'career', id: subject.career_id }
    default: return null
  }
}
