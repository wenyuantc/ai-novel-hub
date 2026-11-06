import type {
  SettingEntry, SettingEntryType, SettingFieldDefinition, SettingFieldDefinitionCreate, SettingFieldOption, SettingFieldType, SettingFieldValue,
} from '../../api/types'
import { SETTING_ENTRY_TYPE_LABELS } from '../../api/settingFields'

export function settingFieldTypeFilter(search: string): SettingEntryType[] {
  const type = new URLSearchParams(search).get('type')
  return Object.keys(SETTING_ENTRY_TYPE_LABELS).filter((value): value is SettingEntryType => value === type && Object.hasOwn(SETTING_ENTRY_TYPE_LABELS, value))
}

export interface DefinitionDraft {
  label: string
  field_type: SettingFieldType
  applicable_types: SettingEntryType[]
  reference_types: SettingEntryType[]
  sort: number | null
  archived: boolean
}
export interface OptionDraft { label: string; sort: number | null; archived: boolean }

export function definitionDraft(item: SettingFieldDefinition | null): DefinitionDraft {
  return { label: item?.label ?? '', field_type: item?.field_type ?? 'text', applicable_types: [...item?.applicable_types ?? []], reference_types: [...item?.reference_types ?? []], sort: item?.sort ?? 0, archived: item?.archived ?? false }
}

const validSort = (value: number | null) => value !== null && Number.isInteger(value) && value >= -2_147_483_648 && value <= 2_147_483_647
const labelError = (label: string) => !label.trim() ? '请填写名称' : [...label.trim()].length > 100 ? '名称最多 100 字' : null

export function definitionDraftError(draft: DefinitionDraft): string | null {
  return labelError(draft.label) ?? (!draft.applicable_types.length ? '请选择至少一种适用类型'
    : draft.field_type === 'reference' && !draft.reference_types.length ? '请选择允许引用的目标类型'
      : !validSort(draft.sort) ? '显示顺序必须是有效整数' : null)
}

export function optionDraftError(draft: OptionDraft): string | null {
  return labelError(draft.label) ?? (!validSort(draft.sort) ? '显示顺序必须是有效整数' : null)
}

export function definitionInput(draft: DefinitionDraft, expectedRevision: number): SettingFieldDefinitionCreate {
  return { expected_fields_revision: expectedRevision, label: draft.label.trim(), field_type: draft.field_type,
    applicable_types: [...draft.applicable_types], reference_types: draft.field_type === 'reference' ? [...draft.reference_types] : [], sort: draft.sort ?? 0, archived: draft.archived }
}

/** 文本原样提交，不裁剪正文；单行限制使用 Unicode 码点。 */
export function fieldValueError(draft: SettingFieldValue): string | null {
  if (draft.field_type !== 'line') return null
  const lineBreaks = new Set(['\r', '\n', '\v', '\f', '\u001c', '\u001d', '\u001e', '\u0085', '\u2028', '\u2029'])
  if ([...draft.value].some((character) => lineBreaks.has(character))) return '单行文本不能包含换行'
  return [...draft.value].length > 500 ? '单行文本最多 500 字' : null
}

/** 只显示选项和目标名称，引用不会带入目标正文。 */
export function formatFieldValue(draft: SettingFieldValue, options: readonly SettingFieldOption[] = [], reference: SettingEntry | null = null): string {
  switch (draft.field_type) {
    case 'text': case 'line': return draft.value || '未填写'
    case 'dropdown': {
      if (draft.value === null) return '未填写'
      const id = draft.value.option_id
      const selected = options.find((item) => item.id === id)
      return selected ? `${selected.label}${selected.archived ? '（已停用）' : ''}` : '选项不可用'
    }
    case 'reference': return draft.value === null ? '未填写' : reference?.reference.kind === draft.value.kind && reference.reference.id === draft.value.id ? reference.name : '引用条目不可用'
  }
}
