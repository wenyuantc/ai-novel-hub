import { Alert, Button, Checkbox, Input, Modal, Space, Typography } from 'antd'
import { useEffect, useId, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import type { SettingTextField, SettingTextFieldInput } from '../../api/types'
import { compareTextField, isStaleRevisionError } from './fields'

interface Props {
  open?: boolean
  field: SettingTextField | null
  timelineRevision: number
  canEdit: boolean
  canEditNow: () => boolean
  onCancel: () => void
  onSubmit: (body: SettingTextFieldInput) => Promise<void>
  onRefresh: (fieldId: number | null) => Promise<{ timelineRevision: number; field: SettingTextField | null }>
}

/** 编辑基础字段，保存冲突时保留草稿并让作者比较服务端内容。 */
export default function TextFieldModal({ open = true, field, timelineRevision, canEdit, canEditNow, onCancel, onSubmit, onRefresh }: Props) {
  const id = useId()
  const [label, setLabel] = useState(field?.label ?? '')
  const [baseValue, setBaseValue] = useState(field?.base_value ?? '')
  const [expectedRevision, setExpectedRevision] = useState(timelineRevision)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [conflict, setConflict] = useState(false)
  const [comparison, setComparison] = useState<'unchanged' | 'changed' | 'deleted' | null>(null)
  const [latest, setLatest] = useState<SettingTextField | null>(null)
  const [acknowledged, setAcknowledged] = useState(false)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const externallyChanged = timelineRevision !== expectedRevision

  const save = async () => {
    if (busy || !canEdit || !canEditNow() || conflict || externallyChanged || comparison === 'deleted' || (comparison === 'changed' && !acknowledged)) return
    const name = label.trim()
    if (!name || Array.from(name).length > 100) { setError('请输入不超过 100 字的字段名称'); return }
    setBusy(true)
    setError(null)
    try {
      // 排序不在此表单编辑；冲突刷新后保留服务端的最新排序。
      await onSubmit({ expected_revision: expectedRevision, label: name, base_value: baseValue, sort: latest?.sort ?? field?.sort ?? 0 })
      if (mounted.current && canEditNow()) onCancel()
    } catch (err) {
      if (!mounted.current || !canEditNow()) return
      setError(errorMessage(err))
      if (isStaleRevisionError(err)) setConflict(true)
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  const refresh = async () => {
    if (busy || !canEditNow()) return
    setBusy(true)
    try {
      const result = await onRefresh(field?.id ?? null)
      if (!mounted.current || !canEditNow()) return
      setExpectedRevision(result.timelineRevision)
      setLatest(result.field)
      setComparison(field ? compareTextField(field, result.field) : 'unchanged')
      setConflict(false)
      setAcknowledged(false)
      setError(null)
    } catch (err) {
      if (mounted.current && canEditNow()) setError(errorMessage(err))
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      title={field ? '编辑自定义字段' : '新增自定义字段'}
      okText="保存字段"
      cancelText="取消"
      onCancel={onCancel}
      onOk={() => void save()}
      confirmLoading={busy}
      okButtonProps={{ disabled: !canEdit || conflict || externallyChanged || comparison === 'deleted' || (comparison === 'changed' && !acknowledged) }}
      cancelButtonProps={{ disabled: busy }}
      closable={!busy}
      maskClosable={!busy}
      width={600}
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Typography.Text type="secondary">这里保存故事开始时的基础内容；章节中的变化请在剧情变化列表中添加。</Typography.Text>
        {error && <Alert type={conflict ? 'warning' : 'error'} showIcon message={error} description={conflict ? '草稿已保留，请读取最新版本并核对。' : undefined} />}
        {externallyChanged && <Alert type="warning" showIcon message="条目已更新，草稿仍保留，请读取最新版本并核对。" />}
        {(conflict || externallyChanged) && <Button onClick={() => void refresh()} loading={busy}>读取最新版本</Button>}
        {comparison === 'deleted' && <Alert type="warning" showIcon message="该字段已被删除。草稿仍保留，请复制后关闭并按需新增。" />}
        {comparison === 'changed' && latest && (
          <Alert
            type="warning"
            showIcon
            message="字段已被修改，请比较当前内容与下方草稿"
            description={(
              <Space direction="vertical">
                <Typography.Text strong>{latest.label}</Typography.Text>
                <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{latest.base_value || '（无基础内容）'}</Typography.Paragraph>
                <Checkbox checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)}>我已比较，确认用草稿覆盖当前字段</Checkbox>
              </Space>
            )}
          />
        )}
        {comparison === 'unchanged' && <Alert type="info" showIcon message="已读取最新版本，请核对保留的草稿后保存。" />}
        <div>
          <label htmlFor={`${id}-label`}>字段名称</label>
          <Input id={`${id}-label`} value={label} disabled={!canEdit || busy} onChange={(event) => setLabel(event.target.value)} placeholder="例如：伤势、持有物" />
        </div>
        <div>
          <label htmlFor={`${id}-value`}>基础内容</label>
          <Input.TextArea id={`${id}-value`} value={baseValue} disabled={!canEdit || busy} onChange={(event) => setBaseValue(event.target.value)} autoSize={{ minRows: 3, maxRows: 12 }} />
        </div>
      </Space>
    </Modal>
  )
}
