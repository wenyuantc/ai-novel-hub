import { Alert, Button, Descriptions, Modal, Space, Switch, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import { aiSettingsNeedsReview, settingAiVisibilityApi } from '../../api/settingAiVisibility'
import type { SettingEntryFields, SettingTextField } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import { authorizationKey } from '../../utils/permissions'
import { projectAccess } from '../../utils/projectAccess'

interface Props {
  projectId: number
  field: SettingTextField
  snapshot: SettingEntryFields
  canEdit: boolean
  canEditNow: () => boolean
  onRefresh: () => Promise<SettingEntryFields>
  onSaved: (revision: number) => void
  onCancel: () => void
}
const hidden = (snapshot: SettingEntryFields, fieldId: number) => snapshot.legacy_fields.find((slot) => slot.field_id === fieldId)?.ai_hidden
const visibilityLabel = (value: boolean) => value ? '对 AI 隐藏' : '可供 AI 使用'

/** 只提交当前旧字段的可见性，字段正文与事件草稿保持独立。 */
export default function LegacyFieldVisibilityModal({ projectId, field, snapshot, canEdit, canEditNow, onRefresh, onSaved, onCancel }: Props) {
  const [expected, setExpected] = useState(snapshot)
  const [value, setValue] = useState(() => hidden(snapshot, field.id) ?? false)
  const [latest, setLatest] = useState<SettingEntryFields | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [needsReview, setNeedsReview] = useState(false)
  const [busy, setBusy] = useState(false)
  const pending = useRef<{ controller: AbortController; writing: boolean } | null>(null)
  useEffect(() => {
    const access = () => {
      const auth = useAuthStore.getState()
      const editable = auth.status === 'authenticated' && projectAccess(useProjectStore.getState().project, auth.user, projectId).canEdit
      return `${auth.status}:${auth.user?.id ?? ''}:${authorizationKey(auth.user)}:${editable}`
    }
    let previous = access()
    const changed = () => {
      const current = access()
      if (current === previous) return
      previous = current
      const request = pending.current
      request?.controller.abort()
      pending.current = null
      setBusy(false)
      setLatest(null)
      if (request?.writing) setNeedsReview(true)
    }
    const stopAuth = useAuthStore.subscribe(changed)
    const stopProject = useProjectStore.subscribe(changed)
    return () => {
      stopAuth(); stopProject()
      pending.current?.controller.abort(); pending.current = null
    }
  }, [projectId])

  const current = hidden(expected, field.id)
  const changedOutside = snapshot.ai_visibility_revision !== expected.ai_visibility_revision || snapshot.timeline_revision !== expected.timeline_revision
  const blocked = needsReview || changedOutside
  const writable = canEdit && !busy && !blocked && current !== undefined && current !== value

  const run = async (action: (signal: AbortSignal) => Promise<void>, writing = false) => {
    if (pending.current || !canEditNow()) return
    const request = { controller: new AbortController(), writing }
    const { signal } = request.controller
    pending.current = request
    setBusy(true); setError(null)
    try { await action(signal) }
    catch (err) {
      if (!signal.aborted && canEditNow()) {
        setError(errorMessage(err))
        if (writing && aiSettingsNeedsReview(err)) setNeedsReview(true)
      }
    } finally {
      if (pending.current === request) { pending.current = null; setBusy(false) }
    }
  }
  const save = () => {
    if (!writable) return
    void run(async (signal) => {
      const saved = await settingAiVisibilityApi.updateLegacyField(projectId, expected.entry.reference, field.id, {
        expected_ai_visibility_revision: expected.ai_visibility_revision,
        expected_timeline_revision: expected.timeline_revision,
        ai_hidden: value,
      }, signal)
      if (!signal.aborted && canEditNow()) onSaved(saved.timeline_revision)
    }, true)
  }
  const compare = () => void run(async (signal) => {
    setLatest(null)
    const next = await onRefresh()
    if (signal.aborted || !canEditNow()) return
    if (hidden(next, field.id) === undefined) throw new Error('此旧字段已被关联或删除，请关闭后刷新条目。')
    setLatest(next)
  })
  const adopt = () => {
    if (!latest || busy || !canEditNow()) return
    setExpected(latest)
    setNeedsReview(false)
    setLatest(null)
    setError(null)
  }

  return <Modal open title={`旧字段 AI 可见性：${field.label}`} okText="保存可见性" cancelText="关闭" closable={!busy} maskClosable={!busy} keyboard={!busy}
    cancelButtonProps={{ disabled: busy }} okButtonProps={{ disabled: !writable }} confirmLoading={busy} onCancel={onCancel} onOk={save}>
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Typography.Text type="secondary">设置只作用于这个字段和它的剧情历史。收紧后，旧会话可能需新开；作者仍可查看原值。</Typography.Text>
      <Space wrap><Switch aria-label="对 AI 隐藏此旧字段" checked={value} onChange={setValue} disabled={!canEdit || busy} />
        <Typography.Text>{visibilityLabel(value)}</Typography.Text></Space>
      {blocked && <Alert type="warning" showIcon message="需要核对最新版本"
        description="未保存的选择仍保留，不会自动覆盖或重发。"
        action={<Button onClick={compare} disabled={!canEdit || busy}>读取并对照</Button>} />}
      {error && <Alert type="error" showIcon message={error} />}
      {latest && <><Descriptions size="small" column={1}>
        <Descriptions.Item label="开始编辑时">{visibilityLabel(current ?? false)}</Descriptions.Item>
        <Descriptions.Item label="当前已保存">{visibilityLabel(hidden(latest, field.id) ?? false)}</Descriptions.Item>
        <Descriptions.Item label="我的选择">{visibilityLabel(value)}</Descriptions.Item>
      </Descriptions><Button disabled={!canEdit || busy} onClick={adopt}>保留我的选择并采用最新版本</Button></>}
    </Space>
  </Modal>
}
