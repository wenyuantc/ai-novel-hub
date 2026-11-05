import { Alert, Button, Checkbox, Modal, Space, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import type { SettingProgression, SettingProgressionInput } from '../../api/types'
import ProgressionEditorFields from './ProgressionEditorFields'
import { buildProgressionInput, compareEditedEvent, isStaleRevisionError, type ProgressionDraft, type ProgressionFieldChoice } from './fields'
import { describeAnchor, type ChapterReference } from './position'

interface Props {
  event: SettingProgression | null
  timelineRevision: number
  fields: readonly ProgressionFieldChoice[]
  chapters: readonly ChapterReference[]
  canEdit: boolean
  canEditNow: () => boolean
  onCancel: () => void
  onSubmit: (input: SettingProgressionInput) => Promise<void>
  onRefresh: (eventId: number | null) => Promise<{ timelineRevision: number; event: SettingProgression | null }>
}

function initialDraft(event: SettingProgression | null, fields: readonly ProgressionFieldChoice[], chapters: readonly ChapterReference[]): ProgressionDraft {
  if (event) return {
    field: event.custom_field_id === null ? `builtin:${event.field_key ?? ''}` : `custom:${event.custom_field_id}`,
    operation: event.operation,
    value: typeof event.value === 'string' ? event.value : '',
    position: { anchor_kind: event.anchor_kind, chapter_id: event.chapter_id, chapter_no: event.chapter_no, boundary: event.boundary },
    sort_order: event.sort_order,
    note: event.note,
  }
  return {
    field: fields[0]?.id ?? null,
    operation: fields[0]?.supportsAppend ? 'append' : 'replace',
    value: '',
    position: { anchor_kind: chapters.length ? 'chapter' : 'planned', chapter_id: null, chapter_no: null, boundary: 'start' },
    sort_order: 0,
    note: '',
  }
}

/** 每次打开时挂载新实例；冲突刷新仅更新版本和对照，不覆盖作者草稿。 */
export default function ProgressionEditModal({ event, timelineRevision, fields, chapters, canEdit, canEditNow, onCancel, onSubmit, onRefresh }: Props) {
  const [draft, setDraft] = useState(() => initialDraft(event, fields, chapters))
  const [expectedRevision, setExpectedRevision] = useState(timelineRevision)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [conflict, setConflict] = useState(false)
  const [comparison, setComparison] = useState<'unchanged' | 'changed' | 'deleted' | null>(null)
  const [latest, setLatest] = useState<SettingProgression | null>(null)
  const [acknowledged, setAcknowledged] = useState(false)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  const save = async () => {
    if (busy || !canEditNow() || conflict || comparison === 'deleted' || (comparison === 'changed' && !acknowledged)) return
    const result = buildProgressionInput(draft, expectedRevision, fields)
    if (!result.ok) { setError(result.error); return }
    setBusy(true)
    setError(null)
    try {
      await onSubmit(result.value)
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
      open
      title={event ? '编辑剧情变化' : '新增剧情变化'}
      onCancel={onCancel}
      onOk={() => void save()}
      okText="保存变化"
      cancelText="取消"
      confirmLoading={busy}
      okButtonProps={{ disabled: !canEdit || conflict || comparison === 'deleted' || (comparison === 'changed' && !acknowledged) }}
      cancelButtonProps={{ disabled: busy }}
      closable={!busy}
      maskClosable={!busy}
      width={600}
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {event?.source === 'analysis' && <Alert type="info" showIcon message="保存后，此变化会作为作者维护的内容，不会被重新分析静默覆盖。" />}
        {event?.orphaned && <Alert type="warning" showIcon message="原关联章节已删除，请选择新的生效位置后保存。" />}
        {error && <Alert type={conflict ? 'warning' : 'error'} showIcon message={error} description={conflict ? '内容已保留。请先读取最新版本并核对，再决定是否保存。' : undefined} />}
        {conflict && <Button onClick={() => void refresh()} loading={busy}>读取最新版本</Button>}
        {comparison === 'deleted' && <Alert type="warning" showIcon message="这条变化已被删除。草稿仍保留，请复制内容后关闭并按需新增。" />}
        {comparison === 'changed' && latest && (
          <Alert
            type="warning"
            showIcon
            message="这条变化在编辑期间已被修改"
            description={(
              <Space direction="vertical">
                <Typography.Text>当前记录：{describeAnchor(latest, chapters)} · {latest.boundary === 'start' ? '从本章起' : '本章结束后'} · {latest.operation === 'append' ? '追加' : '替换'}</Typography.Text>
                <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{typeof latest.value === 'string' ? latest.value : JSON.stringify(latest.value)}</Typography.Paragraph>
                <Checkbox checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)}>我已比较，确认用下方草稿覆盖这条变化</Checkbox>
              </Space>
            )}
          />
        )}
        {comparison === 'unchanged' && <Alert type="info" showIcon message="已读取最新版本，请核对保留的草稿后保存。" />}
        <ProgressionEditorFields draft={draft} fields={fields} chapters={chapters} disabled={!canEdit || busy} onChange={setDraft} />
      </Space>
    </Modal>
  )
}
