import { Alert, Button, Space, Spin, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { errorMessage } from '../../api/client'
import type { ProgressionPositionInput, SettingEffective, SettingEntryFields, SettingEntryRef } from '../../api/types'
import SettingFieldValueView from '../settingFields/SettingFieldValueView'
import { fieldSlotValue } from '../settingFields/valueDraft'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'
import ChapterPositionInput from './ChapterPositionInput'
import type { ProgressionFieldChoice } from './fields'
import { chapterOptions, toPositionInput, type ChapterReference } from './position'
import { effectiveRows, loadEffectivePreview } from './preview'

interface Props {
  projectId: number
  timelineId: number
  timelineRevision: number
  chapters: readonly ChapterReference[]
  fields: readonly ProgressionFieldChoice[]
  onRefresh: () => void
  entry?: SettingEntryRef
  fieldsRevision?: number
  initialPosition?: ProgressionPositionInput | null
}

interface Result {
  key: string
  data: SettingEffective | null
  error: string | null
  entryFields?: SettingEntryFields | null
}

export default function EffectiveSettingPreview({ projectId, timelineId, timelineRevision, chapters, fields, onRefresh, entry, fieldsRevision, initialPosition }: Props) {
  const { canRead, canReadNow } = useProjectAccess(projectId)
  const identity = useAuthStore((state) => `${state.status}:${state.user?.id ?? ''}:${authorizationKey(state.user)}`)
  const [position, setPosition] = useState<ProgressionPositionInput>(() => {
    if (initialPosition) return initialPosition
    const firstChapter = chapterOptions(chapters)[0]?.value ?? null
    return { anchor_kind: firstChapter === null ? 'planned' : 'chapter', chapter_id: firstChapter, chapter_no: null, boundary: 'start' }
  })
  const [result, setResult] = useState<Result | null>(null)
  const [attempt, setAttempt] = useState(0)
  const chapterId = position.anchor_kind === 'chapter' && chapters.some((chapter) => chapter.id === position.chapter_id) ? position.chapter_id : null
  const selection = toPositionInput({ ...position, chapter_id: chapterId })
  const entryKind = entry?.kind, entryId = entry?.id
  const requestKey = selection === null ? null : `${identity}:${projectId}:${timelineId}:${timelineRevision}:${fieldsRevision}:${entryKind}:${entryId}:${selection.anchor_kind}:${selection.chapter_id ?? selection.chapter_no}:${selection.boundary}:${attempt}`
  const current = result?.key === requestKey ? result : null

  useEffect(() => {
    if (!canRead || requestKey === null) return
    let active = true
    const controller = new AbortController()
    const requested: ProgressionPositionInput = {
      anchor_kind: position.anchor_kind, chapter_id: chapterId, chapter_no: position.chapter_no, boundary: position.boundary,
    }
    loadEffectivePreview({ projectId, timelineId, timelineRevision, fieldsRevision, position: requested,
      entry: entryKind && entryId ? { kind: entryKind, id: entryId } : undefined, signal: controller.signal }).then(
      ({ data, entryFields }) => {
        if (!active || !canReadNow()) return
        setResult({ key: requestKey, data, entryFields, error: null })
      },
      (err: unknown) => {
        if (active && canReadNow() && !controller.signal.aborted) setResult({ key: requestKey, data: null, error: errorMessage(err) })
      },
    )
    return () => { active = false; controller.abort() }
  }, [projectId, timelineId, timelineRevision, fieldsRevision, entryKind, entryId, chapterId, position.anchor_kind, position.chapter_no, position.boundary, canRead, canReadNow, requestKey])

  if (!canRead) return null
  const rows = current?.data ? effectiveRows(current.data, fields) : []
  return (
    <Space direction="vertical" size="small" style={{ width: '100%' }}>
      <Typography.Title level={5} style={{ marginBottom: 0 }}>按章节查看有效设定</Typography.Title>
      <Typography.Text type="secondary">这是所选条目的设定内容；生成时还会按选角和上下文预算处理。</Typography.Text>
      <ChapterPositionInput
        chapters={chapters}
        value={{ ...position, chapter_id: chapterId }}
        onChange={setPosition}
        purpose="preview"
      />
      {requestKey !== null && !current && <Spin size="small" aria-label="正在读取有效设定" />}
      {current?.error && (
        <Alert
          type="error"
          showIcon
          message={current.error}
          action={<Button size="small" onClick={() => { setAttempt((value) => value + 1); onRefresh() }}>刷新</Button>}
        />
      )}
      {current?.data && (
        <Space direction="vertical" size="small" style={{ width: '100%' }}>
          {current.data.orphaned_event_ids.length > 0 && <Alert type="warning" showIcon message={`${current.data.orphaned_event_ids.length} 条变化的关联章节已删除，未计入结果。`} />}
          {rows.map((row) => (
            <div key={row.key}>
              <Typography.Text strong>{row.label}</Typography.Text>
              <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{row.value}</Typography.Paragraph>
            </div>
          ))}
          {current.entryFields?.fields.map((slot) => <div key={`definition:${slot.definition_id}`}><Typography.Text strong>{slot.label}</Typography.Text><div><SettingFieldValueView projectId={projectId} value={fieldSlotValue(slot)} options={slot.options} reference={slot.reference} /></div></div>)}
          {rows.length === 0 && !current.entryFields?.fields.length && <Typography.Text type="secondary">此位置暂无内容。</Typography.Text>}
        </Space>
      )}
    </Space>
  )
}
