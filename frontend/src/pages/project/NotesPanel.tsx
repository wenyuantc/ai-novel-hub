import { PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Drawer, Empty, Input, List, Pagination, Space, Spin, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { useLocation, useSearchParams } from 'react-router-dom'
import { noteApi } from '../../api/notes'
import NoteEditor from '../../components/notes/NoteEditor'
import { noteId, noteSearch } from '../../components/notes/noteDraft'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useLoader } from '../../hooks/useLoader'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectId } from '../../hooks/useProjectId'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'
import { formatTime } from '../../utils/time'

/** 独立作品笔记；身份变化重建本地状态，不把上一作者的内容留在界面。 */
export default function NotesPanel() {
  const pid = useProjectId()
  const user = useAuthStore((state) => state.user)
  const { ready, canRead } = useProjectAccess(pid)
  if (!ready) return <Spin />
  if (!canRead) return <Alert type="warning" message="当前账号无权查看作品笔记" />
  return <NotesWorkspace key={`${pid}:${user?.id}:${authorizationKey(user)}`} projectId={pid} />
}

function NotesWorkspace({ projectId: pid }: { projectId: number }) {
  const { canEdit } = useProjectAccess(pid)
  const mobile = useIsMobile()
  const location = useLocation()
  const [searchParams, setSearchParams] = useSearchParams()
  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const id = noteId(searchParams.get('id'))
  const creating = searchParams.get('new') === '1' && !searchParams.has('id')
  const open = creating || searchParams.has('id')
  const go = (target: number | 'new' | null) => setSearchParams(noteSearch(location.search, target))

  return <Space direction="vertical" size={16} style={{ width: '100%' }}>
    <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
      <div>
        <Typography.Title level={3} style={{ margin: 0 }}>作品笔记</Typography.Title>
        <Typography.Text type="secondary">记录零散想法、研究资料和创作待办。</Typography.Text>
      </div>
      {canEdit && <Button type="primary" icon={<PlusOutlined />} onClick={() => go('new')}>新建笔记</Button>}
    </Space>
    <Input.Search placeholder="搜索标题或正文" aria-label="搜索作品笔记" allowClear maxLength={200}
      onSearch={(value) => { setQ(value.trim()); setPage(1) }} style={{ maxWidth: 460 }} />
    <NotesList key={`${q}:${page}:${pageSize}`} projectId={pid} query={q} page={page} pageSize={pageSize}
      onOpen={(target) => go(target)} onPage={(next, size) => { setPage(size === pageSize ? next : 1); setPageSize(size) }} />
    <Drawer title={creating ? '新建笔记' : canEdit ? '编辑笔记' : '查看笔记'} open={open}
      width={mobile ? '100%' : 760} destroyOnHidden onClose={() => go(null)}>
      {creating ? (canEdit ? <NoteEditor key="new" projectId={pid} initial={null} />
        : <Alert type="warning" message="当前账号无权创建笔记" />)
        : id ? <NoteDetail key={id} projectId={pid} id={id} />
          : <Alert type="error" message="笔记链接无效" description="请关闭此窗口，从笔记列表重新打开。" />}
    </Drawer>
  </Space>
}

function NotesList({ projectId: pid, query, page, pageSize, onOpen, onPage }: {
  projectId: number; query: string; page: number; pageSize: number
  onOpen: (id: number) => void; onPage: (page: number, size: number) => void
}) {
  const { canReadNow } = useProjectAccess(pid)
  const load = useCallback(async () => {
    if (!canReadNow()) throw new Error('当前账号无权查看作品笔记')
    const result = await noteApi.list(pid, { q: query, page, page_size: pageSize })
    if (!canReadNow()) throw new Error('作品访问权限已变更')
    return result
  }, [pid, query, page, pageSize, canReadNow])
  const { data, loading, error, reload } = useLoader(load, { projectId: pid, resources: ['notes'] })
  return <Card title={data ? `全部笔记 · ${data.total}` : '全部笔记'} extra={
    <Button icon={<ReloadOutlined />} disabled={loading} onClick={reload}>刷新</Button>
  }>
    {error && <Alert type="error" showIcon message={error} action={<Button onClick={reload}>重试</Button>} style={{ marginBottom: 16 }} />}
    <List loading={loading} dataSource={data?.items ?? []} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
      description={query ? '没有找到相关笔记' : '还没有笔记，点击「新建笔记」开始记录'} /> }}
      renderItem={(note) => <List.Item key={note.id}>
        <div style={{ minWidth: 0, width: '100%' }}>
          <Button type="link" onClick={() => onOpen(note.id)} style={{ padding: 0, maxWidth: '100%', height: 'auto', whiteSpace: 'normal', textAlign: 'left' }}>
            <span style={{ overflowWrap: 'anywhere' }}>{note.title}</span>
          </Button>
          <Typography.Paragraph ellipsis={{ rows: 2 }} type="secondary" style={{ margin: '6px 0', whiteSpace: 'pre-wrap' }}>
            {note.excerpt || '（暂无正文）'}
          </Typography.Paragraph>
          <Typography.Text type="secondary">更新于 {formatTime(note.updated_at)}</Typography.Text>
        </div>
      </List.Item>} />
    {!!data?.total && <Pagination current={page} pageSize={pageSize} total={data.total} showSizeChanger
      pageSizeOptions={[20, 50, 100]} onChange={onPage} style={{ marginTop: 16 }} />}
  </Card>
}

function NoteDetail({ projectId: pid, id }: { projectId: number; id: number }) {
  const { canReadNow } = useProjectAccess(pid)
  const load = useCallback(async () => {
    if (!canReadNow()) throw new Error('当前账号无权查看笔记')
    const note = await noteApi.get(pid, id)
    if (!canReadNow()) throw new Error('作品访问权限已变更')
    return note
  }, [pid, id, canReadNow])
  // 编辑中的正文不订阅自动刷新，防止列表刷新覆盖本地草稿。
  const { data, loading, error, reload } = useLoader(load)
  if (loading) return <Spin />
  if (error) return <Alert type="error" showIcon message={error} action={<Button onClick={reload}>重试</Button>} />
  return data ? <NoteEditor projectId={pid} initial={data} /> : null
}
