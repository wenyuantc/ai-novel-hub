import { lifeStatusLabel } from '../../api/characters'
import { progressionApi } from '../../api/progressions'
import { settingFieldsApi } from '../../api/settingFields'
import type { ProgressionJsonValue, ProgressionPositionInput, SettingEffective, SettingEntryRef } from '../../api/types'
import type { ProgressionFieldChoice } from './fields'

interface EffectivePreviewRequest {
  projectId: number
  timelineId: number
  timelineRevision: number
  fieldsRevision?: number
  entry?: SettingEntryRef
  position: ProgressionPositionInput
  signal: AbortSignal
}

/** 多个HTTP读取结果拼合前，时间线和字段目录版本必须都匹配。 */
export function assertSettingReadRevision(actual: { timeline_revision: number; fields_revision?: number | null }, timelineRevision: number, fieldsRevision?: number) {
  if (actual.timeline_revision !== timelineRevision || (fieldsRevision !== undefined && actual.fields_revision !== fieldsRevision)) {
    throw new Error('设定已更新，请刷新后查看。')
  }
}

/** 同一预览的旧字段与类型化字段必须来自同一组目录和时间线版本。 */
export async function loadEffectivePreview({ projectId, timelineId, timelineRevision, fieldsRevision, entry, position, signal }: EffectivePreviewRequest) {
  const [data, entryFields] = await Promise.all([
    progressionApi.effectiveAtPosition(projectId, timelineId, position, signal),
    entry ? settingFieldsApi.getEntryFields(projectId, entry, position, signal) : Promise.resolve(null),
  ])
  const expectedFieldsRevision = fieldsRevision ?? entryFields?.fields_revision
  assertSettingReadRevision(data, timelineRevision, expectedFieldsRevision)
  if (entryFields) assertSettingReadRevision(entryFields, timelineRevision, expectedFieldsRevision)
  return { data, entryFields }
}

function displayValue(value: ProgressionJsonValue, fieldKey: string | null): string {
  if (typeof value === 'string') {
    if (fieldKey === 'life_status') return lifeStatusLabel(value).label
    return value || '（空）'
  }
  if (value === null) return '（空）'
  return typeof value === 'object' ? '值暂不可用，请刷新' : String(value)
}

export function effectiveRows(value: SettingEffective, fields: readonly ProgressionFieldChoice[]) {
  return fields.flatMap((field) => {
    if (field.slot) return []
    if (field.field_key !== null && Object.hasOwn(value.fields, field.field_key)) {
      return [{ key: field.id, label: field.label, value: displayValue(value.fields[field.field_key], field.field_key) }]
    }
    const id = field.custom_field_id
    if (id !== null && Object.hasOwn(value.custom_fields, id)) {
      return [{ key: field.id, label: field.label, value: value.custom_fields[String(id)] || '（空）' }]
    }
    return []
  })
}
