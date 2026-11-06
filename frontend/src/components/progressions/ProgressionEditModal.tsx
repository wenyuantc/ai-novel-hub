import { Alert, Button, Checkbox, Modal, Space, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import type { SettingProgression, SettingProgressionInput } from '../../api/types'
import ProgressionEditorFields from './ProgressionEditorFields'
import { buildProgressionInput, compareEditedEvent, initialProgressionDraft, isStaleRevisionError, progressionField, progressionReference, progressionValueLabel, type ProgressionFieldChoice } from './fields'
import { fieldValueFromRaw } from '../settingFields/valueDraft'
import { describeAnchor, type ChapterReference } from './position'

interface Props {
  projectId: number
  open?: boolean
  event: SettingProgression | null
  timelineRevision: number
  fieldsRevision?: number
  initialField?: string
  fields: readonly ProgressionFieldChoice[]
  chapters: readonly ChapterReference[]
  canEdit: boolean
  canEditNow: () => boolean
  onCancel: () => void
  onSubmit: (input: SettingProgressionInput, field: ProgressionFieldChoice, comparedEvent: SettingProgression | null) => Promise<void>
  onRefresh: (eventId: number | null) => Promise<{ timelineRevision: number; fieldsRevision?: number; event: SettingProgression | null }>
}

/** 每次打开时挂载新实例；冲突刷新仅更新版本和对照，不覆盖作者草稿。 */
export default function ProgressionEditModal({ projectId, open = true, event, timelineRevision, fieldsRevision, initialField, fields, chapters, canEdit, canEditNow, onCancel, onSubmit, onRefresh }: Props) {
  const [draft, setDraft] = useState(() => initialProgressionDraft(event, initialField ? [...fields.filter((field) => field.id === initialField), ...fields.filter((field) => field.id !== initialField)] : fields, chapters))
  const [expectedRevision, setExpectedRevision] = useState(timelineRevision)
  const [expectedFieldsRevision, setExpectedFieldsRevision] = useState(fieldsRevision)
  const [reviewRequired, setReviewRequired] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [conflict, setConflict] = useState(false)
  const [comparison, setComparison] = useState<'unchanged' | 'changed' | 'deleted' | null>(null)
  const [latest, setLatest] = useState<SettingProgression | null>(null)
  const [acknowledged, setAcknowledged] = useState(false)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const externallyChanged = timelineRevision !== expectedRevision || (fields.find((field) => field.id === draft.field)?.slot && fieldsRevision !== expectedFieldsRevision)

  const save = async () => {
    if (busy || !canEdit || !canEditNow() || conflict || externallyChanged || comparison === 'deleted' || (reviewRequired && !acknowledged)) return
    const comparedEvent = latest ?? event
    const original = comparedEvent?.field_type ? fieldValueFromRaw(comparedEvent.field_type, comparedEvent.value) ?? undefined : undefined
    const result = buildProgressionInput(draft, expectedRevision, fields, expectedFieldsRevision, original)
    if (!result.ok) { setError(result.error); return }
    const selected = fields.find((field) => field.id === draft.field)
    if (!selected) return
    setBusy(true)
    setError(null)
    try {
      await onSubmit(result.value, selected, comparedEvent)
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
      const result = await onRefresh(event?.id ?? null)
      if (!mounted.current || !canEditNow()) return
      const nextComparison = event ? compareEditedEvent(event, result.event) : 'unchanged'
      setExpectedRevision(result.timelineRevision)
      setExpectedFieldsRevision(result.fieldsRevision)
      setReviewRequired(true)
      setLatest(result.event)
      setComparison(nextComparison)
      setAcknowledged(false)
      setConflict(false)
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
      title={event ? '编辑剧情变化' : '新增剧情变化'}
      onCancel={onCancel}
      onOk={() => void save()}
      okText="保存变化"
      cancelText="取消"
      confirmLoading={busy}
      okButtonProps={{ disabled: !canEdit || conflict || !!externallyChanged || comparison === 'deleted' || (reviewRequired && !acknowledged) }}
      cancelButtonProps={{ disabled: busy }}
      closable={!busy}
      maskClosable={!busy}
      width={600}
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {event?.source === 'analysis' && <Alert type="info" showIcon message="保存后，此变化会作为作者维护的内容，不会被重新分析静默覆盖。" />}
        {event?.orphaned && <Alert type="warning" showIcon message="原关联章节已删除，请选择新的生效位置后保存。" />}
        {error && <Alert type={conflict ? 'warning' : 'error'} showIcon message={error} description={conflict ? '内容已保留。请先读取最新版本并核对，再决定是否保存。' : undefined} />}
        {externallyChanged && <Alert type="warning" showIcon message="条目或字段配置已更新，当前草稿仍保留，请读取最新版本并比较。" />}
        {(conflict || externallyChanged) && <Button onClick={() => void refresh()} loading={busy}>读取最新版本</Button>}
        {comparison === 'deleted' && <Alert type="warning" showIcon message="这条变化已被删除。草稿仍保留，请复制内容后关闭并按需新增。" />}
        {comparison === 'changed' && latest && (
          <Alert
            type="warning"
            showIcon
            message="这条变化在编辑期间已被修改"
            description={(
              <Space direction="vertical">
                <Typography.Text>当前记录：{describeAnchor(latest, chapters)} · {latest.boundary === 'start' ? '从本章起' : '本章结束后'} · {latest.operation === 'append' ? '追加' : '替换'}</Typography.Text>
                <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{progressionValueLabel(latest, progressionField(latest, fields))}</Typography.Paragraph>
              </Space>
            )}
          />
        )}
        {comparison === 'unchanged' && <Alert type="info" showIcon message="已读取最新版本，请核对保留的草稿后保存。" />}
        {reviewRequired && <Checkbox checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)}>我已比较最新记录与字段配置，确认保存保留的草稿</Checkbox>}
        <ProgressionEditorFields projectId={projectId} draft={draft} fields={fields} chapters={chapters} disabled={!canEdit || busy} onChange={setDraft} reference={latest ? progressionReference(latest) : event ? progressionReference(event) : null} />
      </Space>
    </Modal>
  )
}
