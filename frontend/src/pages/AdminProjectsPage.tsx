import { DeleteOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Input, Select, Space, Table, Tag, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { adminApi } from '../api/admin'
import { PROJECT_STATUS } from '../api/projects'
import type { AdminProject, ProjectStatus } from '../api/types'
import DeleteProjectModal from '../components/project/DeleteProjectModal'
import { useLoader } from '../hooks/useLoader'
import { formatTime } from '../utils/time'

/** 实例内项目目录；自己的书架仍只查询当前账户。 */
export default function AdminProjectsPage() {
  const [query, setQuery] = useState('')
  const [ownerId, setOwnerId] = useState<number>()
  const [ownerSearch, setOwnerSearch] = useState('')
  const [status, setStatus] = useState<ProjectStatus>()
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [deleting, setDeleting] = useState<AdminProject | null>(null)
  const loader = useCallback(() => adminApi.listProjects({ q: query, owner_user_id: ownerId, status, page, page_size: pageSize }), [query, ownerId, status, page, pageSize])
  const { data, loading, error, reload } = useLoader(loader)
  const ownerLoader = useCallback(() => adminApi.listUsers({ q: ownerSearch, page_size: 20 }), [ownerSearch])
  const owners = useLoader(ownerLoader)
  const columns: TableColumnsType<AdminProject> = [
    { title: '项目名称', dataIndex: 'title', width: 230, render: (title: string, p) => <Link to={`/projects/${p.id}/overview`}>{title || '未命名项目'}</Link> },
    { title: '所属用户', dataIndex: 'owner_username', width: 140 },
    { title: '题材', dataIndex: 'genre', width: 130, render: (v: string) => v || '—' },
    { title: '状态', dataIndex: 'status', width: 105, render: (v: ProjectStatus) => <Tag color={PROJECT_STATUS[v].color}>{PROJECT_STATUS[v].label}</Tag> },
    { title: '章节数', dataIndex: 'chapter_count', width: 85 },
    { title: '总字数', dataIndex: 'total_words', width: 110, render: (v: number) => v.toLocaleString('zh-CN') },
    { title: '创建时间', dataIndex: 'created_at', width: 170, render: (v: string) => formatTime(v) },
    { title: '更新时间', dataIndex: 'updated_at', width: 170, render: (v: string) => formatTime(v) },
    { title: '操作', key: 'actions', width: 150, render: (_, p) => <Space><Link to={`/projects/${p.id}/overview`}>管理</Link><Button type="link" danger size="small" icon={<DeleteOutlined />} onClick={() => setDeleting(p)}>删除</Button></Space> },
  ]
  return <div style={{ maxWidth: 1500, minWidth: 0, margin: '0 auto', padding: 24 }}>
    <Typography.Title level={3} style={{ marginTop: 0 }}>作品管理</Typography.Title>
    <Typography.Paragraph type="secondary">查看实例内的全部项目，进入项目维护资料、正文和设定。</Typography.Paragraph>
    <Card styles={{ body: { padding: 16, minWidth: 0 } }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 16 }}>
        <Input.Search aria-label="搜索项目或用户" allowClear placeholder="搜索项目名称或用户名" maxLength={200} style={{ width: 280, maxWidth: '100%' }} onSearch={(v) => { setQuery(v.trim()); setPage(1) }} />
        <Select<number> aria-label="按所属用户筛选" allowClear showSearch filterOption={false} placeholder="所属用户" value={ownerId} loading={owners.loading} onSearch={(v) => setOwnerSearch(v.slice(0, 64))} onChange={(v) => { setOwnerId(v); setPage(1) }} style={{ width: 200 }} options={(owners.data?.items ?? []).map((u) => ({ value: u.id, label: u.username }))} notFoundContent={owners.error ?? '没有匹配的用户'} />
        <Select<ProjectStatus> aria-label="按项目状态筛选" allowClear placeholder="项目状态" value={status} onChange={(v) => { setStatus(v); setPage(1) }} style={{ width: 140 }} options={(Object.keys(PROJECT_STATUS) as ProjectStatus[]).map((value) => ({ value, label: PROJECT_STATUS[value].label }))} />
      </div>
      {error && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={reload}>重试</Button>} style={{ marginBottom: 16 }} />}
      <Table<AdminProject> rowKey="id" columns={columns} dataSource={data?.items ?? []} loading={loading} scroll={{ x: 1290 }} locale={{ emptyText: '没有匹配的项目' }} pagination={{ current: page, pageSize, total: data?.total ?? 0, showSizeChanger: true, showTotal: (total) => `共 ${total} 个项目`, onChange: (p, size) => { setPage(size !== pageSize ? 1 : p); setPageSize(size) } }} />
    </Card>
    {deleting && <DeleteProjectModal key={deleting.id} project={deleting} onClose={() => setDeleting(null)} onDeleted={() => { setDeleting(null); if (data?.items.length === 1 && page > 1) setPage(page - 1); else reload() }} />}
  </div>
}
