import { Alert, Button, Input, Select, Space } from 'antd'
import { useCallback, useState, type ReactNode } from 'react'
import { settingFieldsApi, SETTING_ENTRY_TYPE_LABELS } from '../../api/settingFields'
import type { SettingEntry, SettingEntryType, SettingFieldSlot, SettingFieldValue } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'
import EntrySelect from '../entities/EntrySelect'
import type { EntrySelectQuery } from '../entities/entrySelectState'
import { entryKey } from './valueDraft'

interface Props {
  projectId: number; slot: SettingFieldSlot; value: SettingFieldValue; onChange: (value: SettingFieldValue) => void
  disabled?: boolean; reference?: SettingEntry | null; id?: string; label?: string
}

/** 只维护受控值，不生成 form，不替调用方保存。 */
export default function SettingFieldValueControl({ projectId, slot, value, onChange, disabled, reference, id, label = slot.label }: Props) {
  if (slot.field_type !== value.field_type) return <Alert type="warning" showIcon message="字段类型已变化，草稿已保留，请重新核对配置。" />
  const locked = disabled || slot.archived
  let selection: ReactNode
  switch (value.field_type) {
    case 'text': return <Input.TextArea id={id} aria-label={label} value={value.value} disabled={locked} autoSize={{ minRows: 3, maxRows: 10 }} onChange={(event) => onChange({ field_type: 'text', value: event.target.value })} />
    case 'line': return <Input id={id} aria-label={label} value={value.value} disabled={locked} onChange={(event) => onChange({ field_type: 'line', value: event.target.value })} />
    case 'dropdown': {
      const selectedId = value.value?.option_id
      const options = slot.options.map((option) => ({ value: option.id, label: `${option.label}${option.archived ? '（已停用）' : ''}`, disabled: option.archived }))
      if (selectedId !== undefined && !options.some((option) => option.value === selectedId)) options.push({ value: selectedId, label: '当前选项不可用', disabled: true })
      selection = <Select id={id} aria-label={label} style={{ width: '100%' }} value={selectedId} options={options} disabled={locked} allowClear placeholder="未填写" onChange={(optionId: number | undefined) => onChange({ field_type: 'dropdown', value: optionId === undefined ? null : { option_id: optionId } })} />
      break
    }
    case 'reference': selection = <ReferenceControl projectId={projectId} types={slot.reference_types} value={value} onChange={onChange} disabled={locked} reference={reference} id={id} label={label} />
  }
  return <Space direction="vertical" size={8} style={{ width: '100%' }}>
    {selection}
    {value.value !== null && <Button size="small" disabled={locked} onClick={() => { if (!locked) onChange({ field_type: value.field_type, value: null }) }}>清空{label}</Button>}
  </Space>
}

interface ReferenceProps extends Pick<Props, 'projectId' | 'disabled' | 'reference' | 'id' | 'label' | 'onChange'> {
  types: SettingEntryType[]; value: Extract<SettingFieldValue, { field_type: 'reference' }>
}
function ReferenceControl({ projectId, types, value, onChange, disabled, reference, id, label = '引用条目' }: ReferenceProps) {
  const identity = useAuthStore((state) => `${state.status}:${state.user?.id ?? ''}:${authorizationKey(state.user)}`)
  const typesKey = JSON.stringify(types)
  const scope = `${projectId}:${identity}:${typesKey}`
  const [page, setPage] = useState<{ scope: string; items: SettingEntry[] } | null>(null)
  const [chosen, setChosen] = useState<{ scope: string; entry: SettingEntry } | null>(null)
  const loadEntries = useCallback(async (query: EntrySelectQuery, signal: AbortSignal) => {
    const result = await settingFieldsApi.listEntries(projectId, { ...query, types }, signal)
    if (!signal.aborted) setPage({ scope, items: result.items })
    return { items: result.items.map((item) => ({ value: entryKey(item.reference), label: `${SETTING_ENTRY_TYPE_LABELS[item.type]} · ${item.name}` })), total: result.total }
  }, [projectId, types, scope])
  const currentKey = value.value ? entryKey(value.value) : null
  const candidates = page?.scope === scope ? page.items : []
  const known = [reference, chosen?.scope === scope ? chosen.entry : null, ...candidates].find((item) => item && entryKey(item.reference) === currentKey)
  return <EntrySelect id={id} projectId={projectId} queryKey={typesKey} loadEntries={loadEntries} single label={label} disabled={disabled}
    value={currentKey ? [currentKey] : []} selectedOptions={currentKey ? [{ value: currentKey, label: known ? `${SETTING_ENTRY_TYPE_LABELS[known.type]} · ${known.name}` : '已选引用（名称待加载）' }] : []}
    onChange={(keys) => {
      if (disabled) return
      const key = keys[0]
      if (key === undefined) { onChange({ field_type: 'reference', value: null }); return }
      const item = candidates.find((candidate) => entryKey(candidate.reference) === key)
      if (item) { setChosen({ scope, entry: item }); onChange({ field_type: 'reference', value: item.reference }) }
    }} />
}
