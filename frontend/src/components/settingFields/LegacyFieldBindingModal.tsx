import { Alert, Button, Checkbox, Modal, Select, Space, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import { settingFieldsApi } from '../../api/settingFields'
import type { SettingEntryFields, SettingFieldDefinition, SettingTextField } from '../../api/types'
import { isStaleRevisionError } from '../progressions/fields'

interface Props {
  projectId: number; field: SettingTextField; snapshot: SettingEntryFields; canEdit: boolean; canEditNow: () => boolean
  onCancel: () => void; onSaved: (revision: number) => void; onRefresh: () => Promise<SettingEntryFields>
}

/** 关联只改变定义身份，不合并同名资料，不覆盖已有绑定和历史。 */
export default function LegacyFieldBindingModal({ projectId, field, snapshot, canEdit, canEditNow, onCancel, onSaved, onRefresh }: Props) {
  const [definitions, setDefinitions] = useState<SettingFieldDefinition[]>([])
  const [expected, setExpected] = useState(snapshot)
  const [selected, setSelected] = useState<number>()
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [conflict, setConflict] = useState(false)
  const [review, setReview] = useState(false)
  const [acknowledged, setAcknowledged] = useState(false)
  const [latestValue, setLatestValue] = useState<string | null>(null)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    const controller = new AbortController()
    void settingFieldsApi.listDefinitions(projectId, { types: [snapshot.entry.type], include_archived: false }, controller.signal).then((data) => {
      if (controller.signal.aborted || !canEditNow()) return
      setDefinitions(data.items)
      if (data.fields_revision !== snapshot.fields_revision) { setConflict(true); setError('字段配置已更新，请读取最新版本并核对') }
    }).catch((err: unknown) => { if (!controller.signal.aborted && canEditNow()) setError(errorMessage(err)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => { mounted.current = false; controller.abort() }
  }, [projectId, snapshot, canEditNow])
  const sourceHidden = expected.legacy_fields.some((slot) => slot.field_id === field.id && slot.ai_hidden)
  const compatible = definitions.filter((item) => item.field_type === 'text' && !item.archived && (!sourceHidden || item.ai_hidden)
    && !expected.fields.some((slot) => slot.definition_id === item.id && slot.field_id !== null))
  const save = async () => {
    if (!canEdit || !canEditNow() || busy || loading || conflict || !selected || !compatible.some((item) => item.id === selected) || (review && !acknowledged)) return
    setBusy(true); setError(null)
    try {
      const result = await settingFieldsApi.bindLegacyField(projectId, expected.entry.reference, field.id, { definition_id: selected, expected_fields_revision: expected.fields_revision, expected_timeline_revision: expected.timeline_revision })
      if (mounted.current && canEditNow()) onSaved(result.timeline_revision)
    } catch (err) { if (mounted.current && canEditNow()) { setError(errorMessage(err)); if (isStaleRevisionError(err)) setConflict(true) } }
    finally { if (mounted.current) setBusy(false) }
  }
  const compare = async () => {
    if (busy || !canEditNow()) return
    setBusy(true)
    try {
      const current = await onRefresh()
      const data = await settingFieldsApi.listDefinitions(projectId, { types: [current.entry.type], include_archived: false })
      if (!mounted.current || !canEditNow()) return
      if (data.fields_revision !== current.fields_revision) throw new Error('配置再次变化，请重新读取')
      const legacy = current.legacy_fields.find((slot) => slot.field_id === field.id)
      if (!legacy) throw new Error('原字段已被关联或删除，请保留草稿后重新打开')
      setLatestValue(typeof legacy.base_value === 'string' ? legacy.base_value : '')
      setExpected(current); setDefinitions(data.items); setConflict(false); setError(null); setReview(true); setAcknowledged(false)
    } catch (err) { if (mounted.current && canEditNow()) setError(errorMessage(err)) }
    finally { if (mounted.current) setBusy(false) }
  }
  return <Modal open title={`关联共享字段：${field.label}`} onCancel={onCancel} onOk={() => void save()} okText="确认关联" cancelText="关闭" confirmLoading={busy} okButtonProps={{ disabled: !canEdit || loading || conflict || !selected || (review && !acknowledged) }} closable={!busy} maskClosable={!busy}>
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Alert type="info" showIcon message="保留原字段身份、起始值和全部章节变化，仅显式关联一个适用的共享多行文本定义。" />
      {sourceHidden && <Typography.Text type="secondary">原字段已对 AI 隐藏，只能关联同样隐藏的共享定义。</Typography.Text>}
      <Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>原起始值：{field.base_value || '未填写'}</Typography.Paragraph>
      <Select aria-label="目标共享文本字段" style={{ width: '100%' }} value={selected} onChange={setSelected} loading={loading} disabled={!canEdit || busy || loading} placeholder="选择兼容的共享文本字段" options={compatible.map((item) => ({ value: item.id, label: item.label }))} />
      {error && <Alert type={conflict ? 'warning' : 'error'} message={error} showIcon />}
      {conflict && <Button onClick={() => void compare()}>读取最新版本并对照</Button>}
      {review && <Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>最新起始值：{latestValue || '未填写'}</Typography.Paragraph>}
      {review && <Checkbox checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)}>已比较最新字段与配置，确认保留原值和历史并关联</Checkbox>}
    </Space>
  </Modal>
}
