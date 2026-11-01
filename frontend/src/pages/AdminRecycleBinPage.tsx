import { DeleteOutlined, ReloadOutlined, UndoOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Empty, Input, List, Pagination, Result, Select, Space, Table, Tag, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { adminApi } from '../api/admin'
import { recycleBinApi } from '../api/recycleBin'
import type { RecycleProject } from '../api/types'
import RecycleProjectModal from '../components/project/RecycleProjectModal'
import type { RecycleAction } from '../components/project/RecycleProjectModal'
import { useIsMobile } from '../hooks/useIsMobile'
import { useLoader } from '../hooks/useLoader'
import { useAuthStore } from '../stores/auth'
import { canRestoreProject, recycleValidPage } from '../utils/recycleBin'
import { formatTime } from '../utils/time'
import { parseAdminUserId } from '../utils/adminUserDetails'

interface ResultsProps {
  items: RecycleProject[]
  mobile: boolean
  loading: boolean
  disabled: boolean
  now: number
  onAction: (project: RecycleProject, action: RecycleAction) => void
}

/** 不展示正文与私人资源；桌面表格和移动卡片共用相同动作与到期判断。 */
export function RecycleProjectResults({ items, mobile, loading, disabled, now, onAction }: ResultsProps) {
  const status = (project: RecycleProject) => canRestoreProject(project, now) ? <Tag color="green">可恢复</Tag> : <Tag color="default">已到期，待清理</Tag>
  const actions = (project: RecycleProject) => <Space wrap size={4}><Button size="small" icon={<UndoOutlined />} disabled={disabled || !canRestoreProject(project, now)} onClick={() => onAction(project, 'restore')}>恢复</Button><Button size="small" danger icon={<DeleteOutlined />} disabled={disabled} onClick={() => onAction(project, 'purge')}>彻底删除</Button></Space>
  const empty = <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="回收站中没有匹配的作品" />
  const columns: TableColumnsType<RecycleProject> = [
    { title: '作品名称', key: 'title', width: 230, render: (_, project) => <span style={{ overflowWrap: 'anywhere' }}><Typography.Text strong>{project.title}</Typography.Text><br /><Typography.Text type="secondary">#{project.id}</Typography.Text></span> },
    { title: '所属用户', key: 'owner', width: 150, render: (_, project) => <>{project.owner_username}<br /><Typography.Text type="secondary">#{project.owner_user_id}</Typography.Text></> },
    { title: '删除人', key: 'actor', width: 140, render: (_, project) => project.deleted_by_username ?? (project.deleted_by_user_id === null ? '未知' : `用户 #${project.deleted_by_user_id}`) },
    { title: '章节 / 字数', key: 'stats', width: 130, render: (_, project) => `${project.chapter_count} 章 / ${project.total_words.toLocaleString('zh-CN')} 字` },
    { title: '删除时间', dataIndex: 'deleted_at', width: 170, render: (value: string) => formatTime(value, 'YYYY-MM-DD HH:mm:ss') },
    { title: '到期时间', dataIndex: 'expires_at', width: 170, render: (value: string) => formatTime(value, 'YYYY-MM-DD HH:mm:ss') },
    { title: '状态', key: 'status', width: 145, render: (_, project) => status(project) },
    { title: '操作', key: 'actions', width: 210, render: (_, project) => actions(project) },
  ]
  if (!mobile) return <Table<RecycleProject> rowKey="id" columns={columns} dataSource={items} loading={loading} pagination={false} scroll={{ x: 1345 }} locale={{ emptyText: empty }} />
  return <List loading={loading} dataSource={items} locale={{ emptyText: empty }} renderItem={(project) => <List.Item key={project.id} style={{ display: 'block', overflowWrap: 'anywhere' }}>
    <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start', justifyContent: 'space-between' }}><Typography.Text strong>{project.title}</Typography.Text>{status(project)}</div>
    <Typography.Paragraph style={{ margin: '8px 0 4px' }}>作品 #{project.id} · 所属用户：{project.owner_username} · #{project.owner_user_id}<br />删除人：{project.deleted_by_username ?? '未知'}<br />{project.chapter_count} 章 · {project.total_words.toLocaleString('zh-CN')} 字</Typography.Paragraph>
    <Typography.Paragraph type="secondary" style={{ marginBottom: 12 }}>删除：{formatTime(project.deleted_at, 'YYYY-MM-DD HH:mm:ss')}<br />到期：{formatTime(project.expires_at, 'YYYY-MM-DD HH:mm:ss')}</Typography.Paragraph>
    {actions(project)}
  </List.Item>} />
}

function RecycleBin({ actorId, initialOwner }: { actorId: number; initialOwner: number | undefined }) {
  const mobile = useIsMobile()
  const [query, setQuery] = useState('')
  const [ownerId, setOwnerId] = useState<number | undefined>(initialOwner)
  const [ownerSearch, setOwnerSearch] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [target, setTarget] = useState<{ project: RecycleProject; action: RecycleAction } | null>(null)
  const [now, setNow] = useState(Date.now)
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer) }, [])
  const loader = useCallback(async () => {
    const result = await recycleBinApi.list({ q: query, owner_user_id: ownerId, page, page_size: pageSize })
    const validPage = recycleValidPage(page, result.total, pageSize)
    return validPage === page ? { ...result, page } : { ...await recycleBinApi.list({ q: query, owner_user_id: ownerId, page: validPage, page_size: pageSize }), page: validPage }
  }, [query, ownerId, page, pageSize])
  const { data, loading, error, reload } = useLoader(loader)
  const currentPage = data?.page ?? page
  const ownerLoader = useCallback(() => adminApi.listUsers({ q: ownerSearch, page_size: 20 }), [ownerSearch])
  const owners = useLoader(ownerLoader)
  const ownerOptions = (owners.data?.items ?? []).map((user) => ({ value: user.id, label: user.username }))
  if (ownerId && !ownerOptions.some((option) => option.value === ownerId)) ownerOptions.unshift({ value: ownerId, label: `用户 #${ownerId}` })
  return <div style={{ maxWidth: 1500, minWidth: 0, margin: '0 auto', padding: mobile ? 12 : 24 }}>
    <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12, alignItems: 'flex-start', marginBottom: 16 }}>
      <div><Typography.Title level={3} style={{ margin: '0 0 8px' }}>作品回收站</Typography.Title><Typography.Text type="secondary">仅管理员可恢复或彻底删除。作品保留 30 天，到期后自动清理；恢复不会自动续跑任务。时间按浏览器本地时区显示。</Typography.Text></div>
      <Space wrap><Link to="/admin/projects">返回作品管理</Link><Button icon={<ReloadOutlined />} onClick={reload} disabled={loading}>刷新</Button></Space>
    </div>
    <Card styles={{ body: { padding: mobile ? 12 : 16, minWidth: 0 } }}>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 16 }}>
        <Input.Search aria-label="搜索回收站作品或用户" placeholder="搜索作品名称或用户名" allowClear maxLength={200} style={{ width: 280, maxWidth: '100%' }} onSearch={(value) => { setQuery(value.trim()); setPage(1); reload() }} />
        <Select<number> aria-label="回收站所属用户筛选" allowClear showSearch filterOption={false} placeholder="所属用户" value={ownerId} loading={owners.loading} onSearch={(value) => setOwnerSearch(value.slice(0, 64))} onChange={(value) => { setOwnerId(value); setPage(1); reload() }} options={ownerOptions} style={{ width: 200, maxWidth: '100%' }} notFoundContent={owners.error ?? '没有匹配的用户'} />
      </div>
      {error && <Alert showIcon type="error" message={error} action={<Button size="small" onClick={reload}>重试</Button>} style={{ marginBottom: 16 }} />}
      <RecycleProjectResults items={data?.items ?? []} mobile={mobile} loading={loading} disabled={target !== null || loading} now={now} onAction={(project, action) => setTarget({ project, action })} />
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, justifyContent: 'space-between', alignItems: 'center', marginTop: 16 }}>
        <Typography.Text type="secondary">共 {data?.total ?? 0} 部作品 · 删除时间与 ID 倒序</Typography.Text>
        <Pagination current={currentPage} pageSize={pageSize} total={data?.total ?? 0} simple={mobile} showSizeChanger={!mobile} pageSizeOptions={[20, 50, 100]} onChange={(next, size) => { setPage(size !== pageSize ? 1 : next); setPageSize(size); reload() }} />
      </div>
    </Card>
    {target && <RecycleProjectModal key={`${actorId}:${target.project.id}:${target.project.deleted_at}:${target.action}`} actorId={actorId} project={target.project} action={target.action} now={now} onClose={() => setTarget(null)} onChanged={() => { setTarget(null); setPage(currentPage); reload() }} />}
  </div>
}

/** 按真实管理员重建数据和待确认记录，旧会话响应不会操作新会话。 */
export default function AdminRecycleBinPage() {
  const user = useAuthStore((state) => state.user)
  const [searchParams] = useSearchParams()
  const owner = parseAdminUserId(searchParams.get('owner_user_id'))
  return user?.is_admin ? <RecycleBin key={`${user.id}:${owner ?? 'all'}`} actorId={user.id} initialOwner={owner ?? undefined} /> : <Result status="403" title="需要管理员权限" />
}
