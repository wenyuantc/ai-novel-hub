import { ApiError } from '../../api/client'
import { LIFE_STATUS } from '../../api/characters'
import type {
  ProgressionOperation,
  ProgressionPositionInput,
  SettingProgressionInput,
  SettingProgression,
  SettingTextField,
  SettingFieldSlot,
  SettingFieldValue,
  SettingFieldProgressionCreate,
  SettingProgressionUpdate,
  SettingEntry,
} from '../../api/types'
import { emptyFieldValue, fieldValueFromRaw, sameFieldValue, validateEntryFieldValue } from '../settingFields/valueDraft'
import { formatFieldValue } from '../settings/settingFieldDraft'
import { toPositionInput, type ChapterReference } from './position'

type TextSubjectKind = 'character' | 'world' | 'organization' | 'career_entry'
type FieldControl = 'multiline' | 'short_text' | 'life_status' | 'typed'

export interface ProgressionFieldChoice {
  id: string
  label: string
  field_key: string | null
  custom_field_id: number | null
  control: FieldControl
  supportsAppend: boolean
  maxLength?: number
  slot?: SettingFieldSlot
}

export interface ProgressionDraft {
  field: string | null
  operation: ProgressionOperation
  value: SettingFieldValue['value']
  position: ProgressionPositionInput
  sort_order: number | null
  note: string
}

const field = (
  key: string, label: string, control: FieldControl, supportsAppend: boolean, maxLength?: number,
): ProgressionFieldChoice => ({
  id: `builtin:${key}`, label, field_key: key, custom_field_id: null, control, supportsAppend, maxLength,
})

const BUILTIN_FIELDS: Record<TextSubjectKind, readonly ProgressionFieldChoice[]> = {
  world: [field('content', '内容', 'multiline', true)],
  character: [
    field('profile', '简介', 'multiline', true),
    field('appearance', '外貌', 'multiline', true),
    field('personality', '性格', 'multiline', true),
    field('background', '背景', 'multiline', true),
    field('current_state', '当前状态', 'multiline', true),
    field('age', '年龄', 'short_text', false, 50),
    field('gender', '性别', 'short_text', false, 20),
    field('life_status', '存活状态', 'life_status', false),
  ],
  organization: [], career_entry: [],
}

export function fieldChoices(kind: TextSubjectKind, custom: readonly SettingTextField[], shared: readonly SettingFieldSlot[] = []): ProgressionFieldChoice[] {
  return [
    ...BUILTIN_FIELDS[kind],
    ...custom.map((item): ProgressionFieldChoice => ({
      id: `custom:${item.id}`, label: item.label, field_key: null, custom_field_id: item.id,
      control: 'multiline', supportsAppend: true,
    })),
    ...shared.map((slot): ProgressionFieldChoice => ({
      id: `definition:${slot.definition_id}`, label: slot.label, field_key: null, custom_field_id: slot.field_id,
      control: 'typed', supportsAppend: slot.field_type === 'text', slot,
    })),
  ]
}

export function buildProgressionInput(
  draft: ProgressionDraft,
  expectedRevision: number,
  fields: readonly ProgressionFieldChoice[],
  expectedFieldsRevision?: number,
  originalValue?: SettingFieldValue,
): { ok: true; value: SettingProgressionInput } | { ok: false; error: string } {
  const selected = fields.find((item) => item.id === draft.field)
  if (!selected) return { ok: false, error: '请选择有效的变化字段' }
  const position = toPositionInput(draft.position)
  if (!position) return { ok: false, error: '请选择章节或输入有效的计划章节号' }
  if (!Number.isSafeInteger(expectedRevision) || expectedRevision < (selected.slot?.field_id === null ? 0 : 1)) return { ok: false, error: '设定版本无效，请刷新后重试' }
  if (selected.slot && (expectedFieldsRevision === undefined || !Number.isSafeInteger(expectedFieldsRevision) || expectedFieldsRevision < 0)) return { ok: false, error: '字段配置版本无效，请刷新后重试' }
  if (draft.operation === 'append' && (!selected.supportsAppend || typeof draft.value !== 'string' || !draft.value.trim())) {
    return { ok: false, error: selected.supportsAppend ? '追加内容不能为空' : '此字段只支持替换' }
  }
  if (selected.slot) {
    const value = fieldValueFromRaw(selected.slot.field_type, draft.value)
    if (!value) return { ok: false, error: '字段值与字段类型不匹配' }
    const validation = validateEntryFieldValue(value, selected.slot, originalValue)
    if (validation) return { ok: false, error: validation }
  } else if (typeof draft.value !== 'string') return { ok: false, error: '此字段需要文本值' }
  if (selected.control === 'life_status' && (typeof draft.value !== 'string' || !Object.hasOwn(LIFE_STATUS, draft.value))) {
    return { ok: false, error: '请选择有效的存活状态' }
  }
  if (selected.maxLength !== undefined && typeof draft.value === 'string' && Array.from(draft.value).length > selected.maxLength) {
    return { ok: false, error: `${selected.label}不能超过 ${selected.maxLength} 字` }
  }
  if (draft.sort_order === null || !Number.isSafeInteger(draft.sort_order) || draft.sort_order < 0 || draft.sort_order > 2_147_483_647) {
    return { ok: false, error: '同章顺序应为非负整数' }
  }
  if (Array.from(draft.note).length > 200) return { ok: false, error: '备注不能超过 200 字' }
  return {
    ok: true,
    value: {
      expected_revision: expectedRevision,
      ...(selected.slot ? { expected_fields_revision: expectedFieldsRevision } : {}),
      field_key: selected.field_key,
      custom_field_id: selected.custom_field_id,
      operation: draft.operation,
      value: draft.value !== null && typeof draft.value === 'object' ? { ...draft.value } : draft.value,
      ...position,
      sort_order: draft.sort_order,
      note: draft.note,
    },
  }
}

export function changeProgressionField(draft: ProgressionDraft, selected: ProgressionFieldChoice): ProgressionDraft {
  return { ...draft, field: selected.id, operation: selected.supportsAppend ? draft.operation : 'replace', value: emptyFieldValue(selected.slot?.field_type ?? 'text').value }
}

export function progressionField(event: SettingProgression, fields: readonly ProgressionFieldChoice[]) {
  return fields.find((choice) => event.custom_field_id !== null ? choice.custom_field_id === event.custom_field_id : choice.field_key === event.field_key)
}

export function initialProgressionDraft(event: SettingProgression | null, fields: readonly ProgressionFieldChoice[], chapters: readonly ChapterReference[]): ProgressionDraft {
  const selected = event ? progressionField(event, fields) : fields.find((choice) => !choice.slot?.archived)
  const typed = event ? fieldValueFromRaw(event.field_type ?? selected?.slot?.field_type ?? 'text', event.value) : null
  return {
    field: selected?.id ?? null, operation: event?.operation ?? (selected?.supportsAppend ? 'append' : 'replace'),
    value: typed ? typed.value : event ? '' : emptyFieldValue(selected?.slot?.field_type ?? 'text').value,
    position: event ? { anchor_kind: event.anchor_kind, chapter_id: event.chapter_id, chapter_no: event.chapter_no, boundary: event.boundary }
      : { anchor_kind: chapters.length ? 'chapter' : 'planned', chapter_id: null, chapter_no: null, boundary: 'start' },
    sort_order: event?.sort_order ?? 0, note: event?.note ?? '',
  }
}

/** 首事件始终走复合入口，definition_id 不能冒充 custom_field_id。 */
export function sharedProgressionCommand(input: SettingProgressionInput, slot: SettingFieldSlot): { definitionId: number; body: SettingFieldProgressionCreate } {
  const typed = fieldValueFromRaw(slot.field_type, input.value)
  if (!typed || slot.definition_id === null || typeof input.expected_fields_revision !== 'number') throw new Error('请选择共享字段的有效值与配置版本')
  if (slot.archived) throw new Error('此字段已停用，不能新增变化')
  return { definitionId: slot.definition_id, body: {
    ...typed, expected_fields_revision: input.expected_fields_revision, expected_timeline_revision: input.expected_revision,
    anchor_kind: input.anchor_kind, chapter_id: input.chapter_id, chapter_no: input.chapter_no, boundary: input.boundary,
    operation: input.operation, sort_order: input.sort_order, note: input.note,
  } }
}

/** 旧停用选择只改备注或章锚时省略 value；显式 null 仍是一次清空。 */
export function progressionUpdateInput(input: SettingProgressionInput, event: SettingProgression, choice: ProgressionFieldChoice): SettingProgressionUpdate {
  const patch: SettingProgressionUpdate = { ...input }
  const before = choice.slot ? fieldValueFromRaw(choice.slot.field_type, event.value) : null
  const after = choice.slot ? fieldValueFromRaw(choice.slot.field_type, input.value) : null
  if (before && after && sameFieldValue(before, after)) delete patch.value
  return patch
}

/** 所有事件显示类型化名称，不能把对象或内部标识串到正文。 */
export function progressionValueLabel(event: SettingProgression, choice?: ProgressionFieldChoice): string {
  const typed = fieldValueFromRaw(event.field_type ?? choice?.slot?.field_type ?? 'text', event.value)
  if (!typed) return '值暂不可用，请刷新字段配置'
  return formatFieldValue(typed, choice?.slot?.options, progressionReference(event))
}

export function progressionReference(event: SettingProgression): SettingEntry | null {
  const typed = fieldValueFromRaw('reference', event.value)
  return typed?.field_type === 'reference' && typed.value && event.reference_name && event.reference_type
    ? { reference: typed.value, name: event.reference_name, type: event.reference_type, category: null } : null
}

/** 刷新冲突后用事件本身的修订号判断，避免用新时间线版本覆盖他人的修改。 */
export function compareEditedEvent(
  original: Pick<SettingProgression, 'id' | 'revision' | 'active'>,
  latest: Pick<SettingProgression, 'id' | 'revision' | 'active'> | null,
): 'unchanged' | 'changed' | 'deleted' {
  if (!latest || latest.id !== original.id || !latest.active) return 'deleted'
  return latest.revision === original.revision ? 'unchanged' : 'changed'
}

export function compareTextField(original: SettingTextField, latest: SettingTextField | null): 'unchanged' | 'changed' | 'deleted' {
  if (!latest || latest.id !== original.id) return 'deleted'
  return latest.label === original.label && latest.base_value === original.base_value && latest.sort === original.sort
    ? 'unchanged'
    : 'changed'
}

/** 只有修订号过期的 409 需要刷新对照；字段不可改等其它 409 按普通错误展示，避免误导作者反复刷新重试。 */
export function isStaleRevisionError(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409 && err.body?.code === 'stale_revision'
}
