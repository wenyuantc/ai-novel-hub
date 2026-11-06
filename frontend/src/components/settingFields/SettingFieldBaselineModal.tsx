import { Alert, Button, Checkbox, Modal, Space, Typography } from 'antd'
import { useEffect, useId, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import type { SettingEntryFields, SettingFieldSlot, SettingFieldValue } from '../../api/types'
import { isStaleRevisionError } from '../progressions/fields'
import SettingFieldValueControl from './SettingFieldValueControl'
import SettingFieldValueView from './SettingFieldValueView'
import { fieldSlotValue, validateEntryFieldValue } from './valueDraft'

interface Props {
  projectId: number; open?: boolean; slot: SettingFieldSlot; snapshot: SettingEntryFields; canEdit: boolean; canEditNow: () => boolean
  onCancel: () => void; onSubmit: (value: SettingFieldValue, expected: SettingEntryFields) => Promise<void>
  onRefresh: () => Promise<SettingEntryFields>
}

/** 起始值单独保存；目录或时间线冲突必须明确对照，不能悄悄换版本重发。 */
export default function SettingFieldBaselineModal({ projectId, open = true, slot, snapshot, canEdit, canEditNow, onCancel, onSubmit, onRefresh }: Props) {
  const id = useId()
  const [draft, setDraft] = useState(() => fieldSlotValue(slot, true))
  const [expected, setExpected] = useState(snapshot)
  const [latest, setLatest] = useState<SettingFieldSlot | null>(slot)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [conflict, setConflict] = useState(false)
  const [comparison, setComparison] = useState(false)
  const [acknowledged, setAcknowledged] = useState(false)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const externallyChanged = snapshot.fields_revision !== expected.fields_revision || snapshot.timeline_revision !== expected.timeline_revision
  const blocked = !canEdit || busy || conflict || externallyChanged || !latest || (comparison && !acknowledged)
  const save = async () => {
    if (blocked || !canEditNow() || !latest) return
    const validation = validateEntryFieldValue(draft, latest, fieldSlotValue(latest, true))
    if (validation) { setError(validation); return }
    setBusy(true); setError(null)
    try {
      await onSubmit(draft, expected)
      if (mounted.current && canEditNow()) onCancel()
    } catch (err) {
      if (mounted.current && canEditNow()) { setError(errorMessage(err)); if (isStaleRevisionError(err)) setConflict(true) }
    } finally { if (mounted.current) setBusy(false) }
  }
  const compare = async () => {
    if (busy || !canEditNow()) return
    setBusy(true)
    try {
      const data = await onRefresh()
      if (!mounted.current || !canEditNow()) return
      setExpected(data); setLatest(data.fields.find((field) => field.definition_id === slot.definition_id) ?? null)
      setComparison(true); setAcknowledged(false); setConflict(false); setError(null)
    } catch (err) { if (mounted.current && canEditNow()) setError(errorMessage(err)) }
    finally { if (mounted.current) setBusy(false) }
  }
  return <Modal open={open} title={`${canEdit ? '编辑' : '查看'}起始值：${slot.label}`} onCancel={onCancel} onOk={() => void save()} okText="保存起始值" cancelText="关闭" confirmLoading={busy} okButtonProps={{ disabled: blocked }} cancelButtonProps={{ disabled: busy }} closable={!busy} maskClosable={!busy} width={600}>
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Typography.Text type="secondary">故事开始时的基础值。按章变化和外层资料表单分别保存。</Typography.Text>
      {error && <Alert type={conflict ? 'warning' : 'error'} showIcon message={error} description="草稿已保留，未自动重试。" />}
      {externallyChanged && <Alert type="warning" showIcon message="条目值或字段配置已更新，草稿已保留，请读取最新版本并对照。" />}
      {(conflict || externallyChanged) && <Button onClick={() => void compare()} loading={busy}>读取最新版本并对照</Button>}
      {comparison && latest && <Alert type="warning" showIcon message="请比较最新起始值与保留的草稿" description={<Space direction="vertical"><SettingFieldValueView projectId={projectId} value={fieldSlotValue(latest, true)} options={latest.options} reference={latest.base_reference} /><Checkbox checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)}>我已核对最新值与字段配置，确认保存下方草稿</Checkbox></Space>} />}
      {!latest && <Alert type="warning" showIcon message="字段已不再适用或已删除，草稿仍保留。" />}
      <label htmlFor={id}>{slot.label}</label>
      <SettingFieldValueControl projectId={projectId} id={id} slot={latest ?? slot} value={draft} onChange={setDraft} disabled={!canEdit || busy} reference={slot.base_reference} />
    </Space>
  </Modal>
}
