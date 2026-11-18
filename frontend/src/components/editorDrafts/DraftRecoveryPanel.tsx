import { Alert, Button, Drawer, Empty, List, Popconfirm, Space, Spin, Tag, Typography, theme } from 'antd'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { downloadJson } from '../../api/cards'
import { countWords } from '../../api/chapters'
import { ApiError, errorMessage } from '../../api/client'
import { sceneApi } from '../../api/scenes'
import type { Scene } from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useAuthStore } from '../../stores/auth'
import { captureProjectScope, isProjectScopeCurrent, useProjectStore, type ProjectScope } from '../../stores/project'
import { editorDraftStorage } from '../../utils/editorDraftStorage'
import { formatTime } from '../../utils/time'
import { draftRecordRef, isTitleDraft, sameEditorDraftScope, type DraftRecordRef, type EditorDraftEntry, type EditorDraftRecord, type EditorDraftScope } from './editorDraftRecord'
import RichDraftDiff from './RichDraftDiff'
import { verifyDraftScope } from './verifyDraftScope'

interface Props {
  projectId: number
  chapterId?: number
  currentSceneId?: number
  currentDraftIds?: readonly string[]
  initialDraftId?: string
  open: boolean
  onClose: () => void
  onRestore?: (record: EditorDraftRecord) => Promise<boolean>
  disabled?: boolean
}

function entryRef(entry: EditorDraftEntry): DraftRecordRef {
  return entry.status === 'ready' ? draftRecordRef(entry.record) : entry.ref
}

function sameRef(a: DraftRecordRef, b: DraftRecordRef) {
  return a.draft_id === b.draft_id && a.writer_id === b.writer_id && a.record_seq === b.record_seq
}

function draftLabel(entry: EditorDraftEntry) {
  if (entry.status === 'unreadable') return '记录无法读取'
  const record = entry.record
  if (isTitleDraft(record)) return record.payload.value || '（空章节标题）'
  const { title, content, summary } = record.payload.draft
  if (title) return title
  const text = (content || summary).slice(0, 160).trim().split(/\r?\n/, 1)[0]
  const characters = Array.from(text)
  return characters.length > 36 ? `${characters.slice(0, 36).join('')}…` : text || '未命名场景'
}

function draftTime(entry: EditorDraftEntry) {
  const savedAt = entry.status === 'ready' ? entry.record.saved_at : entry.saved_at
  if (savedAt === null) return '保存时间未知'
  const date = new Date(savedAt)
  return Number.isNaN(date.getTime()) ? '保存时间未知' : formatTime(date.toISOString(), 'YYYY-MM-DD HH:mm:ss')
}

function hasPendingAttempt(record: EditorDraftRecord, scene: Scene | null) {
  if (!scene) return false
  const revision = isTitleDraft(record) ? record.payload.attempt?.revision : record.payload.attempt?.baseRevision
  return revision !== undefined && scene.content_revision <= revision
}

function DraftPreview({ record }: { record: EditorDraftRecord }) {
  if (isTitleDraft(record)) return <Typography.Paragraph style={{ overflowWrap: 'anywhere' }}>{record.payload.value || '（空章节标题）'}</Typography.Paragraph>
  return <Space direction="vertical" style={{ width: '100%', overflowWrap: 'anywhere' }}>
    {record.payload.draft.summary && <div>
      <Typography.Text strong>摘要</Typography.Text>
      <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', maxHeight: 180, overflowY: 'auto' }}>{record.payload.draft.summary}</Typography.Paragraph>
    </div>}
    {record.payload.documentDraft ? <RichDraftDiff local={record.payload.documentDraft} /> : (
      <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', maxHeight: 280, overflowY: 'auto' }}>{record.payload.draft.content || '（空正文）'}</Typography.Paragraph>
    )}
  </Space>
}

interface Listing { entries: EditorDraftEntry[]; loading: boolean; error?: string }
interface InspectedDraft { ref: DraftRecordRef; scene: Scene | null }

function RecoveryDrawer({ props, scope, owner }: { props: Props; scope: ProjectScope; owner: EditorDraftScope }) {
  const { projectId, chapterId, currentDraftIds, initialDraftId, onClose, onRestore, currentSceneId, disabled } = props
  const navigate = useNavigate()
  const { token } = theme.useToken()
  const [listing, setListing] = useState<Listing>({ entries: [], loading: true })
  const [selected, setSelected] = useState<InspectedDraft | null>(null)
  const [operationKey, setOperationKey] = useState<string | null>(null)
  const [notice, setNotice] = useState('')
  const [reload, setReload] = useState(0)
  const lifecycle = useRef<AbortController | null>(null)
  const busy = useRef(false)
  const checking = useRef(0)
  const verified = useRef(false)
  const canRead = () => isProjectScopeCurrent(scope) && useProjectStore.getState().project?.created_at === owner.project_created_at
    && useAuthStore.getState().user?.created_at === owner.actor_created_at

  useEffect(() => {
    const abort = new AbortController()
    lifecycle.current = abort
    const { signal } = abort
    const current = () => !signal.aborted && isProjectScopeCurrent(scope)
      && useProjectStore.getState().project?.created_at === owner.project_created_at
      && useAuthStore.getState().user?.created_at === owner.actor_created_at
    let authorityCheck = 0
    let readSeq = 0
    let timeout: ReturnType<typeof setTimeout> | undefined

    const read = async () => {
      const request = ++readSeq
      try {
        const entries = await editorDraftStorage.list(owner, () => verified.current && current(), chapterId)
        if (verified.current && current() && request === readSeq) setListing({ entries, loading: false })
      } catch (error) {
        if (verified.current && current() && request === readSeq) setListing({ entries: [], loading: false, error: errorMessage(error) })
      }
    }
    const verify = async () => {
      const request = ++authorityCheck
      const panelVersion = ++checking.current
      verified.current = false
      const valid = () => request === authorityCheck && current() && panelVersion === checking.current
      const confirmed = await verifyDraftScope(scope, valid, signal)
      if (!valid()) return
      if (!confirmed) {
        setListing({ entries: [], loading: false, error: '请联网确认登录和作品访问权限后，再查看本地草稿。' })
        return
      }
      verified.current = true
      await read()
    }
    const recheck = () => {
      if (document.visibilityState === 'hidden' || !current()) return
      setSelected(null)
      setListing({ entries: [], loading: true })
      void verify()
    }
    const visibilityChanged = () => {
      if (document.visibilityState !== 'hidden') { recheck(); return }
      verified.current = false
      authorityCheck += 1
      checking.current += 1
      setSelected(null)
      setListing({ entries: [], loading: true })
    }
    const unsubscribe = editorDraftStorage.subscribe((event) => {
      if (!verified.current || !sameEditorDraftScope(event.scope, owner) || (chapterId !== undefined && chapterId !== event.chapter_id)) return
      clearTimeout(timeout)
      timeout = setTimeout(() => void read(), 120)
    })
    const unsubscribeErrors = editorDraftStorage.subscribeError((error) => {
      if (!verified.current || !current()) return
      readSeq += 1
      setSelected(null)
      setListing({ entries: [], loading: false, error: errorMessage(error) })
    })
    void verify()
    document.addEventListener('visibilitychange', visibilityChanged)
    window.addEventListener('focus', recheck)
    return () => {
      abort.abort()
      clearTimeout(timeout)
      unsubscribe()
      unsubscribeErrors()
      window.removeEventListener('focus', recheck)
      document.removeEventListener('visibilitychange', visibilityChanged)
    }
  }, [chapterId, owner, reload, scope])

  const inspectTarget = async (entry: EditorDraftEntry, isCurrent: () => boolean, signal: AbortSignal) => {
    if (entry.status === 'unreadable') return null
    const { target } = entry.record
    try {
      const scene = await sceneApi.get(projectId, target.chapter_id, target.anchor_scene_id, signal, isCurrent)
      return scene.id === target.anchor_scene_id && scene.chapter_id === target.chapter_id && scene.active
        && (target.kind === 'chapter_title' || scene.created_at === target.scene_created_at) ? scene : null
    } catch (error) {
      if (error instanceof ApiError && error.status === 404 && await verifyDraftScope(scope, isCurrent, signal)) return null
      throw error
    }
  }

  const operate = async (entry: EditorDraftEntry, action: 'view' | 'download' | 'remove' | 'restore') => {
    const signal = lifecycle.current?.signal
    const authorityCheck = checking.current
    const current = () => Boolean(signal && !signal.aborted && authorityCheck === checking.current && canRead())
    if (!signal || !current() || busy.current) return
    const ref = entryRef(entry)
    busy.current = true
    setOperationKey(ref.draft_id)
    setNotice('')
    try {
      if (!(await verifyDraftScope(scope, current, signal))) {
        if (current()) {
          verified.current = false
          setSelected(null)
          setListing({ entries: [], loading: false, error: '请联网确认登录和作品访问权限后，再查看本地草稿。' })
        }
        return
      }
      const raw = await editorDraftStorage.readRaw(owner, ref, current)
      if (!current()) return
      if (raw === null) {
        setSelected(null)
        setNotice('草稿已更新。请重新选择要查看的版本。')
        setReload((value) => value + 1)
        return
      }
      if (action === 'download') {
        downloadJson(raw, `本地草稿-${ref.draft_id.replace(/[^\w-]/g, '_')}.json`)
        return
      }
      const scene = await inspectTarget(entry, current, signal)
      if (!current()) return
      if (action === 'view') {
        setSelected({ ref, scene })
        return
      }
      if (currentDraftIds?.includes(ref.draft_id)) return
      if (action === 'remove') {
        if (entry.status === 'ready' && hasPendingAttempt(entry.record, scene)) {
          setNotice('请先到章节编辑器核对上一次保存结果。')
          return
        }
        const removed = await editorDraftStorage.remove(owner, ref, current)
        if (!current()) return
        setSelected(null)
        setNotice(removed ? '已舍弃所选草稿。' : '草稿已更新。请重新选择要舍弃的版本。')
        setReload((value) => value + 1)
        return
      }
      if (disabled || entry.status !== 'ready' || !isProjectScopeCurrent(scope, 'canEdit')) return
      if (!scene) {
        setSelected({ ref, scene: null })
        return
      }
      if (onRestore && chapterId === scene.chapter_id && currentSceneId === scene.id) {
        if (await onRestore(entry.record)) { if (current()) onClose() }
        else if (current()) setNotice('未恢复草稿。请查看编辑器中的保存状态。')
      } else {
        const query = new URLSearchParams({ scene_id: String(scene.id), draft: ref.draft_id })
        navigate(`/projects/${projectId}/chapters/${scene.chapter_id}?${query}`)
      }
    } catch (error) {
      if (current()) {
        if (action === 'view' || action === 'restore') setSelected(null)
        setNotice(errorMessage(error))
      }
    } finally {
      busy.current = false
      if (!signal.aborted) setOperationKey(null)
    }
  }

  const selectedEntry = selected && listing.entries.find((entry) => sameRef(entryRef(entry), selected.ref))
  const canEdit = isProjectScopeCurrent(scope, 'canEdit')

  return (
    <Drawer title="本地草稿" open width="min(640px, 100vw)" onClose={onClose}>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {listing.loading && <Spin tip="正在核对权限并读取草稿"><div style={{ minHeight: 56 }} /></Spin>}
        {listing.error && <Alert type="warning" showIcon message={listing.error} action={<Button size="small" onClick={() => { setListing({ loading: true, entries: [] }); setReload((value) => value + 1) }}>重试</Button>} />}
        {notice && <Typography.Text role="status">{notice}</Typography.Text>}
        {!listing.loading && !listing.error && listing.entries.length === 0 && <Empty description="暂无本地草稿" />}
        {listing.entries.length > 0 && <List size="small" rowKey={(entry) => entryRef(entry).draft_id} dataSource={listing.entries}
          pagination={listing.entries.length > 8 ? { pageSize: 8, size: 'small', showSizeChanger: false } : false}
          renderItem={(entry) => {
            const key = entryRef(entry).draft_id
            const active = selected !== null && sameRef(entryRef(entry), selected.ref)
            const record = entry.status === 'ready' ? entry.record : null
            const target = record?.target ?? (entry.status === 'unreadable' ? entry.target : null)
            const characters = record ? countWords(isTitleDraft(record) ? record.payload.value : record.payload.draft.content) : null
            return <List.Item style={{ gap: 8, flexWrap: 'wrap', background: active || key === initialDraftId ? token.colorFillQuaternary : undefined }}>
              <Space direction="vertical" size={2} style={{ flex: '1 1 180px', minWidth: 0, overflowWrap: 'anywhere' }}>
                <Typography.Text strong>{draftLabel(entry)}</Typography.Text>
                <Typography.Text type="secondary">{target ? target.kind === 'chapter_title' ? '章节标题' : '场景正文' : '草稿目标未知'} · {draftTime(entry)}{characters !== null ? ` · ${characters} 字` : ''}</Typography.Text>
                <Space size={4} wrap>
                  {record?.recovery_hold && <Tag>恢复稿待保存</Tag>}
                  {record?.payload.attempt && <Tag color="warning">上次保存待核对</Tag>}
                  {currentDraftIds?.includes(key) && <Tag>当前编辑</Tag>}
                </Space>
              </Space>
              <Button size="small" aria-expanded={active} disabled={operationKey !== null} loading={operationKey === key} onClick={() => void operate(entry, 'view')}>查看</Button>
            </List.Item>
          }} />}
        {selected && !selectedEntry && listing.entries.length > 0 && <Alert type="info" message="所选草稿已更新，请重新查看。" />}
        {selected && selectedEntry && <div style={{ borderTop: `1px solid ${token.colorBorderSecondary}`, paddingBlockStart: 12 }}>
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Typography.Text strong>{draftLabel(selectedEntry)}</Typography.Text>
            {selectedEntry.status === 'ready' ? <>
              {!selected.scene && <Alert type="warning" showIcon message="原章节或场景已失效，可以下载草稿保留内容。" />}
              {hasPendingAttempt(selectedEntry.record, selected.scene) && <Alert type="info" message="请先到编辑器核对上一次保存结果，再决定是否舍弃草稿。" />}
              {selected.scene && <Typography.Text type="secondary">第 {selected.scene.chapter_no} 章{selected.scene.chapter_title ? ` ${selected.scene.chapter_title}` : ''} · 场景 {selected.scene.sort + 1}{selected.scene.title ? ` · ${selected.scene.title}` : ''}</Typography.Text>}
              <DraftPreview record={selectedEntry.record} />
            </> : <Alert type="warning" showIcon message="无法读取这份草稿。你可以下载原始记录或舍弃。" />}
            <Space wrap>
              {selected.scene && canEdit && !currentDraftIds?.includes(selected.ref.draft_id) && <Button type="primary" disabled={disabled || operationKey !== null}
                onClick={() => void operate(selectedEntry, 'restore')}>{onRestore && chapterId === selected.scene.chapter_id && currentSceneId === selected.scene.id ? '恢复到编辑区' : '打开对应章节'}</Button>}
              <Button disabled={operationKey !== null} onClick={() => void operate(selectedEntry, 'download')}>下载完整草稿</Button>
              {!currentDraftIds?.includes(selected.ref.draft_id) && (selectedEntry.status === 'unreadable' || !hasPendingAttempt(selectedEntry.record, selected.scene)) && (
                <Popconfirm title="舍弃这份本地草稿？" description="舍弃后将无法从本机恢复这份草稿。" okText="舍弃" cancelText="取消" okButtonProps={{ danger: true }} onConfirm={() => operate(selectedEntry, 'remove')}>
                  <Button danger disabled={operationKey !== null}>舍弃</Button>
                </Popconfirm>
              )}
            </Space>
          </Space>
        </div>}
      </Space>
    </Drawer>
  )
}

/** 关闭面板或登录／授权代次变化时卸载已显示的草稿。 */
export default function DraftRecoveryPanel(props: Props) {
  const { open, projectId } = props
  const actorId = useAuthStore((state) => state.user?.id)
  const actorCreatedAt = useAuthStore((state) => state.user?.created_at)
  const projectCreatedAt = useProjectStore((state) => state.project?.created_at)
  const scopeVersion = useProjectStore((state) => state.scopeVersion)
  const { ready, canRead } = useProjectAccess(projectId)
  const binding = useMemo(() => {
    if (!open || !ready || !canRead || actorId === undefined || !actorCreatedAt || !projectCreatedAt) return null
    const scope = captureProjectScope(projectId)
    if (!scope || scope.version !== scopeVersion || scope.actorId !== actorId) return null
    const owner: EditorDraftScope = { actor_id: actorId, actor_created_at: actorCreatedAt, project_id: projectId, project_created_at: projectCreatedAt }
    return { scope, owner }
  }, [open, projectId, ready, canRead, actorId, actorCreatedAt, projectCreatedAt, scopeVersion])
  if (!binding) return null
  const { scope, owner } = binding
  return <RecoveryDrawer key={`${scope.identity}:${scope.version}:${owner.project_id}:${owner.project_created_at}:${owner.actor_created_at}`} scope={scope} owner={owner} props={props} />
}
