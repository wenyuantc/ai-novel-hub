import { DeleteOutlined, SaveOutlined } from '@ant-design/icons'
import { Alert, App, Button, Form, Input, Modal, Popconfirm, Segmented, Space, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { Navigate, useBlocker, useLocation } from 'react-router-dom'
import { errorMessage } from '../../api/client'
import { isNoteConflict, noteApi, type NoteInput, type ProjectNote } from '../../api/notes'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { invalidateProject } from '../../utils/projectRefresh'
import { formatTime } from '../../utils/time'
import { noteDirty, noteDraftError, noteSearch, noteTargetKey } from './noteDraft'
import { readMentionAnchor } from '../../utils/mentions'
import MentionPreview from '../mentions/MentionPreview'

interface Props { projectId: number; initial: ProjectNote | null }

/** 笔记正文只在明确保存时提交；修订冲突和切换视图都保留当前草稿。 */
export default function NoteEditor({ projectId: pid, initial }: Props) {
  const { message } = App.useApp()
  const { canEdit, canReadNow, canEditNow } = useProjectAccess(pid)
  const location = useLocation()
  const mentionAnchor = readMentionAnchor(new URLSearchParams(location.search))
  const [saved, setSaved] = useState(initial)
  const [draft, setDraft] = useState<NoteInput>({ title: initial?.title ?? '', content: initial?.content ?? '' })
  const [view, setView] = useState({ search: '', preview: false })
  const preview = view.search === location.search ? view.preview : !!mentionAnchor
  const setPreview = (value: boolean) => setView({ search: location.search, preview: value })
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const [error, setError] = useState<string | null>(null)
  const [conflict, setConflict] = useState(false)
  const [reloadConfirm, setReloadConfirm] = useState(false)
  const [pendingSearch, setPendingSearch] = useState<string | null>(null)
  const dirty = noteDirty(draft, saved)
  const guard = pendingSearch === null && dirty
  const blocker = useBlocker(({ currentLocation, nextLocation }) => guard
    && noteTargetKey(currentLocation.pathname, currentLocation.search) !== noteTargetKey(nextLocation.pathname, nextLocation.search))

  useEffect(() => {
    if (!guard) return
    const beforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', beforeUnload)
    return () => window.removeEventListener('beforeunload', beforeUnload)
  }, [guard])

  const save = async () => {
    if (busyRef.current || !canEditNow()) return
    const invalid = noteDraftError(draft)
    if (invalid) { setError(invalid); return }
    busyRef.current = true
    setBusy(true)
    setError(null)
    try {
      const body = { title: draft.title.trim(), content: draft.content }
      const updated = saved
        ? await noteApi.update(pid, saved.id, { ...body, expected_revision: saved.revision })
        : await noteApi.create(pid, body)
      if (!canEditNow()) return
      setSaved(updated)
      setDraft({ title: updated.title, content: updated.content })
      setConflict(false)
      invalidateProject(pid, ['notes'])
      message.success('笔记已保存')
      if (!saved) setPendingSearch(noteSearch(location.search, updated.id))
    } catch (err) {
      if (!canReadNow()) return
      setConflict(isNoteConflict(err))
      setError(errorMessage(err))
    } finally {
      busyRef.current = false
      if (canReadNow()) setBusy(false)
    }
  }

  const loadLatest = async () => {
    setReloadConfirm(false)
    if (!saved || busyRef.current || !canReadNow()) return
    busyRef.current = true
    setBusy(true)
    try {
      const latest = await noteApi.get(pid, saved.id)
      if (!canReadNow()) return
      setSaved(latest)
      setDraft({ title: latest.title, content: latest.content })
      setConflict(false)
      setError(null)
    } catch (err) {
      if (canReadNow()) setError(errorMessage(err))
    } finally {
      busyRef.current = false
      if (canReadNow()) setBusy(false)
    }
  }

  const remove = async () => {
    if (!saved || busyRef.current || !canEditNow()) return
    busyRef.current = true
    setBusy(true)
    setError(null)
    try {
      await noteApi.remove(pid, saved.id, saved.revision)
      if (!canEditNow()) return
      invalidateProject(pid, ['notes'])
      message.success('笔记已删除')
      setPendingSearch(noteSearch(location.search, null))
    } catch (err) {
      if (!canReadNow()) return
      setConflict(isNoteConflict(err))
      setError(errorMessage(err))
    } finally {
      busyRef.current = false
      if (canReadNow()) setBusy(false)
    }
  }

  if (pendingSearch !== null) return <Navigate to={{ pathname: location.pathname, search: pendingSearch }} replace />

  return <div onKeyDown={(event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
      event.preventDefault()
      void save()
    }
  }}>
    <Space wrap style={{ marginBottom: 16, width: '100%', justifyContent: 'space-between' }}>
      {canEdit ? <Segmented options={[{ label: '编辑', value: 'edit' }, { label: '查看原文', value: 'preview' }]}
        value={preview ? 'preview' : 'edit'} onChange={(value) => setPreview(value === 'preview')} />
        : <Typography.Text type="secondary">只读查看</Typography.Text>}
      <Space wrap>
        <Typography.Text type="secondary">{dirty ? '有未保存修改' : saved ? `已保存 · ${formatTime(saved.updated_at)}` : '新笔记'}</Typography.Text>
        {canEdit && <Button type="primary" icon={<SaveOutlined />} loading={busy} disabled={!dirty && !!saved}
          onClick={() => { void save() }}>保存</Button>}
      </Space>
    </Space>
    {error && <Alert type={conflict ? 'warning' : 'error'} showIcon message={error}
      description={conflict ? '你的草稿仍保留，可复制内容后加载最新版本再修改。' : undefined}
      action={conflict && saved ? <Button size="small" disabled={busy} onClick={() => setReloadConfirm(true)}>加载最新版本</Button> : undefined}
      style={{ marginBottom: 16 }} />}
    {canEdit && !preview ? <Form layout="vertical">
      <Form.Item label="标题" htmlFor="note-title" required>
        <Input id="note-title" value={draft.title} disabled={busy} placeholder="例如：人物线索、调查资料、后续想法"
          onChange={(event) => setDraft((value) => ({ ...value, title: event.target.value }))} />
      </Form.Item>
      <Form.Item label="正文" htmlFor="note-content" extra="纯文本笔记，最多 200000 字。保存后参与提及追踪。">
        <Input.TextArea id="note-content" value={draft.content} disabled={busy} autoSize={{ minRows: 14, maxRows: 28 }}
          placeholder="记录想法、研究材料或创作待办……"
          onChange={(event) => setDraft((value) => ({ ...value, content: event.target.value }))} />
      </Form.Item>
    </Form> : <section aria-label="笔记原文">
      <Typography.Title level={4} style={{ overflowWrap: 'anywhere' }}>{draft.title || '未命名笔记'}</Typography.Title>
      <div style={{ lineHeight: 1.8 }}><MentionPreview projectId={pid} text={draft.content} anchor={mentionAnchor} draft={dirty} /></div>
    </section>}
    {canEdit && saved && <Popconfirm title={`删除「${saved.title}」？`} description="笔记及未保存的修改将被删除。"
      okText="删除" cancelText="取消" okButtonProps={{ danger: true }} disabled={busy} onConfirm={remove}>
      <Button danger icon={<DeleteOutlined />} disabled={busy} style={{ marginTop: 24 }}>删除笔记</Button>
    </Popconfirm>}
    <Modal title="放弃修改并加载最新版本？" open={reloadConfirm} onCancel={() => setReloadConfirm(false)}
      onOk={() => { void loadLatest() }} okText="放弃修改并加载" cancelText="保留草稿">
      当前未保存的内容会被最新版本替换。
    </Modal>
    <Modal title="笔记有未保存修改" open={blocker.state === 'blocked'} okText="放弃修改并离开" cancelText="继续编辑"
      okButtonProps={{ danger: true, disabled: busy }} onCancel={() => { if (blocker.state === 'blocked') blocker.reset() }}
      onOk={() => { if (blocker.state === 'blocked') blocker.proceed() }}>
      {busy ? '正在保存，请等待请求完成。' : '离开将丢弃当前笔记的未保存修改。'}
    </Modal>
  </div>
}
