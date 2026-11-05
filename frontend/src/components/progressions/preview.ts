import { lifeStatusLabel } from '../../api/characters'
import type { ProgressionJsonValue, SettingEffective } from '../../api/types'
import type { ProgressionFieldChoice } from './fields'

function displayValue(value: ProgressionJsonValue, fieldKey: string | null): string {
  if (typeof value === 'string') {
    if (fieldKey === 'life_status') return lifeStatusLabel(value).label
    return value || '（空）'
  }
  if (value === null) return '（空）'
  return typeof value === 'object' ? JSON.stringify(value) : String(value)
}

export function effectiveRows(value: SettingEffective, fields: readonly ProgressionFieldChoice[]) {
  return fields.flatMap((field) => {
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
