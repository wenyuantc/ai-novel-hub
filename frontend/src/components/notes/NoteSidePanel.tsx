import { CloseOutlined, PlusOutlined, PushpinFilled, ReloadOutlined, StarFilled } from '@ant-design/icons'
import { Alert, App, Button, Empty, Select, Space, Spin, Typography, theme } from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import { noteApi, type NoteSummary, type ProjectNote } from '../../api/notes'
import { useLoader } from '../../hooks/useLoader'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'
import NoteEditor, { type NoteEditorEmbedded } from './NoteEditor'
import { sideNoteSwitch, type SideNoteSelection } from './noteDraft'

const PINNED_LIMIT = 100

interface Props {
  projectId: number
  /** 必须为稳定引用；卸载时会回报 false。 */
  onDirtyChange: (dirty: boolean) => void
}

/**
 * 章节编辑页的笔记栏：固定笔记快速切换、搜索打开任意笔记、新建，并嵌入可独立保存的笔记编辑器。
 * 选中项只保存在内存；宿主须保持本组件挂载（隐藏而非卸载）以免丢失草稿。
 */
export default function NoteSidePanel({ projectId: pid, onDirtyChange }: Props) {
  const user = useAuthStore((state) => state.user)
  const { ready, canRead } = useProjectAccess(pid)
  if (!ready) return <Spin />
  if (!canRead) return <Alert type="warning" message="当前账号无权查看作品笔记" />
  return <SideWorkspace key={`${pid}:${user?.id}:${authorizationKey(user)}`} projectId={pid} onDirtyChange={onDirtyChange} />
}

function SideWorkspace({ projectId: pid, onDirtyChange }: Props) {
  const { modal } = App.useApp()
  const { token } = theme.useToken()
  const { canEdit, canReadNow } = useProjectAccess(pid)
  const [selection, setSelection] = useState<SideNoteSelection | null>(null)
  const [dirty, setDirty] = useState(false)
  const newSeq = useRef(0)

  const loadPinned = useCallback(async () => {
    if (!canReadNow()) throw new Error('当前账号无权查看作品笔记')
    const result = await noteApi.list(pid, { pinned: true, page: 1, page_size: PINNED_LIMIT })
    if (!canReadNow()) throw new Error('作品访问权限已变更')
    return result
  }, [pid, canReadNow])
  const pinned = useLoader(loadPinned, { projectId: pid, resources: ['notes'] })

  const reportDirty = useCallback((value: boolean) => {
    setDirty(value)
    onDirtyChange(value)
  }, [onDirtyChange])
  useEffect(() => () => onDirtyChange(false), [onDirtyChange])

  const open = (target: number | 'new' | null) => {
    const action = sideNoteSwitch(selection, target, dirty)
    if (action === 'same') return
    const apply = () => {
      if (target === null) setSelection(null)
      else if (target === 'new') setSelection({ key: `new:${++newSeq.current}`, id: null })
      else setSelection({ key: `note:${target}`, id: target })
    }
    if (action === 'switch') { apply(); return }
    modal.confirm({
      title: '笔记有未保存修改',
      content: target === null ? '关闭后将丢弃当前笔记的未保存修改。' : '切换后将丢弃当前笔记的未保存修改。',
      okText: target === null ? '放弃修改并关闭' : '放弃修改并切换',
      cancelText: '继续编辑',
      okButtonProps: { danger: true },
      onOk: apply,
    })
  }

  const embedded = useMemo<NoteEditorEmbedded>(() => ({
    onDirtyChange: reportDirty,
    onSaved: (note) => setSelection((value) => value && value.id !== note.id ? { ...value, id: note.id } : value),
    onDeleted: () => setSelection(null),
  }), [reportDirty])

  const items = pinned.data?.items ?? []
  // 笔记栏内任意位置的 Ctrl/⌘+S 都不冒泡到编辑页的正文保存；编辑器自身已先处理保存。
  return <div onKeyDown={(event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') { event.preventDefault(); event.stopPropagation() }
  }}><Space direction="vertical" size={12} style={{ width: '100%' }}>
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between' }}>
        <Typography.Text strong><PushpinFilled style={{ color: token.colorPrimary }} /> 固定笔记{pinned.data ? ` · ${pinned.data.total}` : ''}</Typography.Text>
        <Button type="text" size="small" icon={<ReloadOutlined />} aria-label="刷新固定笔记" disabled={pinned.loading} onClick={pinned.reload} />
      </Space>
      {pinned.error && <Alert type="error" showIcon message={pinned.error} style={{ marginTop: 8 }}
        action={<Button size="small" onClick={pinned.reload}>重试</Button>} />}
      {pinned.loading && !pinned.data ? <Spin size="small" style={{ display: 'block', marginTop: 8 }} />
        : !pinned.error && (items.length === 0
          ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} style={{ margin: '8px 0' }}
            description="还没有固定笔记；在笔记上点击图钉即可固定到这里" />
          : <ul aria-label="固定笔记" style={{ listStyle: 'none', padding: 0, margin: '8px 0 0', maxHeight: 220, overflowY: 'auto' }}>
            {items.map((note) => <PinnedItem key={note.id} note={note} active={selection?.id === note.id} onOpen={() => open(note.id)} />)}
          </ul>)}
      {pinned.data && pinned.data.total > items.length && <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        仅显示前 {PINNED_LIMIT} 篇固定笔记，其余可通过下方搜索打开。
      </Typography.Text>}
    </div>
    <Space.Compact style={{ width: '100%' }}>
      <NoteSearchSelect projectId={pid} onPick={(id) => open(id)} />
      {canEdit && <Button icon={<PlusOutlined />} onClick={() => open('new')}>新建</Button>}
    </Space.Compact>
    {selection ? <div style={{ borderTop: `1px solid ${token.colorBorderSecondary}`, paddingTop: 12 }}>
      <Space style={{ width: '100%', justifyContent: 'flex-end', marginBottom: 4 }}>
        <Button type="text" size="small" icon={<CloseOutlined />} onClick={() => open(null)}>关闭笔记</Button>
      </Space>
      {selection.id === null
        ? <NoteEditor key={selection.key} projectId={pid} initial={null} embedded={embedded} />
        : <SideNoteDetail key={selection.key} projectId={pid} id={selection.id} embedded={embedded} />}
    </div> : <Typography.Text type="secondary">选择一篇固定笔记、搜索其他笔记{canEdit ? '或新建笔记' : ''}，即可在写作时对照查看。</Typography.Text>}
  </Space></div>
}

function PinnedItem({ note, active, onOpen }: { note: NoteSummary; active: boolean; onOpen: () => void }) {
  const { token } = theme.useToken()
  return <li>
    <Button type="text" block onClick={onOpen} aria-current={active ? 'true' : undefined}
      style={{ justifyContent: 'flex-start', textAlign: 'left', height: 'auto', padding: '4px 8px', background: active ? token.colorPrimaryBg : undefined }}>
      <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{note.title}</span>
      {note.is_favorite && <StarFilled aria-label="已收藏" style={{ color: token.colorWarning, flex: 'none' }} />}
    </Button>
  </li>
}

/** 远程搜索任意笔记；只采纳最新一次请求的结果。 */
function NoteSearchSelect({ projectId: pid, onPick }: { projectId: number; onPick: (id: number) => void }) {
  const { canReadNow } = useProjectAccess(pid)
  const [options, setOptions] = useState<{ value: number; label: string }[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const seq = useRef(0)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { seq.current++; if (timer.current) clearTimeout(timer.current) }, [])

  const search = (q: string) => {
    if (timer.current) clearTimeout(timer.current)
    const current = ++seq.current
    setLoading(true)
    timer.current = setTimeout(() => {
      noteApi.list(pid, { q, page: 1, page_size: 20 }).then((result) => {
        if (current !== seq.current || !canReadNow()) return
        setOptions(result.items.map((note) => ({ value: note.id, label: note.title })))
        setError(null)
      }, (err: unknown) => {
        if (current !== seq.current || !canReadNow()) return
        setOptions([])
        setError(errorMessage(err))
      }).finally(() => { if (current === seq.current && canReadNow()) setLoading(false) })
    }, 250)
  }

  return <Select<number> showSearch value={null} placeholder="搜索并打开其他笔记" aria-label="搜索并打开其他笔记"
    style={{ flex: 1, minWidth: 0 }} filterOption={false} loading={loading} options={options}
    notFoundContent={loading ? <Spin size="small" /> : error ?? '没有找到笔记'}
    onOpenChange={(visible) => { if (visible) search('') }}
    onSearch={search} onChange={(id) => { if (typeof id === 'number') onPick(id) }} />
}

function SideNoteDetail({ projectId: pid, id, embedded }: { projectId: number; id: number; embedded: NoteEditorEmbedded }) {
  const { canReadNow } = useProjectAccess(pid)
  const load = useCallback(async (): Promise<ProjectNote> => {
    if (!canReadNow()) throw new Error('当前账号无权查看笔记')
    const note = await noteApi.get(pid, id)
    if (!canReadNow()) throw new Error('作品访问权限已变更')
    return note
  }, [pid, id, canReadNow])
  // 编辑中的笔记不订阅自动刷新，避免固定列表刷新覆盖本地草稿。
  const { data, loading, error, reload } = useLoader(load)
  if (loading) return <Spin />
  if (error) return <Alert type="error" showIcon message={error} action={<Button size="small" onClick={reload}>重试</Button>} />
  return data ? <NoteEditor projectId={pid} initial={data} embedded={embedded} /> : null
}
