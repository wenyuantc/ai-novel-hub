import { ApiError } from '../../api/client'
import { LIFE_STATUS } from '../../api/characters'
import type {
  ProgressionOperation,
  ProgressionPositionInput,
  SettingProgressionInput,
  SettingProgression,
  SettingTextField,
} from '../../api/types'
import { toPositionInput } from './position'

type TextSubjectKind = 'character' | 'world'
type FieldControl = 'multiline' | 'short_text' | 'life_status'

export interface ProgressionFieldChoice {
  id: string
  label: string
  field_key: string | null
  custom_field_id: number | null
  control: FieldControl
  supportsAppend: boolean
  maxLength?: number
}

export interface ProgressionDraft {
  field: string | null
  operation: ProgressionOperation
  value: string
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
}

export function fieldChoices(kind: TextSubjectKind, custom: readonly SettingTextField[]): ProgressionFieldChoice[] {
  return [
    ...BUILTIN_FIELDS[kind],
    ...custom.map((item): ProgressionFieldChoice => ({
      id: `custom:${item.id}`, label: item.label, field_key: null, custom_field_id: item.id,
      control: 'multiline', supportsAppend: true,
    })),
  ]
}

export function buildProgressionInput(
  draft: ProgressionDraft,
  expectedRevision: number,
  fields: readonly ProgressionFieldChoice[],
): { ok: true; value: SettingProgressionInput } | { ok: false; error: string } {
  const selected = fields.find((item) => item.id === draft.field)
  if (!selected) return { ok: false, error: '请选择有效的变化字段' }
  const position = toPositionInput(draft.position)
  if (!position) return { ok: false, error: '请选择章节或输入有效的计划章节号' }
  if (!Number.isSafeInteger(expectedRevision) || expectedRevision < 1) return { ok: false, error: '设定版本无效，请刷新后重试' }
  if (draft.operation === 'append' && (!selected.supportsAppend || !draft.value.trim())) {
    return { ok: false, error: selected.supportsAppend ? '追加内容不能为空' : '此字段只支持替换' }
  }
  if (selected.control === 'life_status' && !Object.hasOwn(LIFE_STATUS, draft.value)) {
    return { ok: false, error: '请选择有效的存活状态' }
  }
  if (selected.maxLength !== undefined && Array.from(draft.value).length > selected.maxLength) {
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
      field_key: selected.field_key,
      custom_field_id: selected.custom_field_id,
      operation: draft.operation,
      value: draft.value,
      ...position,
      sort_order: draft.sort_order,
      note: draft.note,
    },
  }
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
