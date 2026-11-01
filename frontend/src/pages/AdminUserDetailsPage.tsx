import { ArrowLeftOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Descriptions, Empty, List, Pagination, Result, Skeleton, Space, Table, Tag, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { useCallback, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { adminApi } from '../api/admin'
import { ApiError, errorMessage } from '../api/client'
import { PROJECT_STATUS } from '../api/projects'
import type { AdminProject, AdminUserDetail } from '../api/types'
import AdminUserActionButtons from '../components/adminUsers/AdminUserActionButtons'
import AdminUserActionDialog from '../components/adminUsers/AdminUserActionDialog'
import { useAdminUserActions } from '../components/adminUsers/useAdminUserActions'
import { useIsMobile } from '../hooks/useIsMobile'
import { useLoader } from '../hooks/useLoader'
import { useAuthStore } from '../stores/auth'
import { parseAdminUserId } from '../utils/adminUserDetails'
import { formatTime } from '../utils/time'

interface SummaryProps { user: AdminUserDetail; actorId: number; mobile: boolean }

/** 仅展示账号元数据及当前正文计数，不读取或渲染私人配置和正文。 */
export function AdminUserSummary({ user, actorId, mobile }: SummaryProps) {
  return <Space direction="vertical" size={16} style={{ width: '100%', minWidth: 0 }}>
    <Descriptions title="账号信息" bordered size="small" column={mobile ? 1 : 3}>
      <Descriptions.Item label="用户 ID">#{user.id}</Descriptions.Item>
      <Descriptions.Item label="用户名">{user.username} {user.id === actorId && <Tag color="blue">我</Tag>}</Descriptions.Item>
      <Descriptions.Item label="角色">{user.is_admin ? <Tag color="gold">管理员</Tag> : <Tag>普通用户</Tag>}</Descriptions.Item>
      <Descriptions.Item label="状态">{user.is_active ? <Tag color="green">启用</Tag> : <Tag color="red">已禁用</Tag>}</Descriptions.Item>
      <Descriptions.Item label="注册时间">{formatTime(user.created_at)}</Descriptions.Item>
      <Descriptions.Item label="最近登录">{user.last_login_at === null ? '暂无记录' : formatTime(user.last_login_at)}</Descriptions.Item>
      <Descriptions.Item label="全部作品数" span={mobile ? 1 : 3}>{user.project_count.toLocaleString('zh-CN')} <Typography.Text type="secondary">（含正常、回收站及删除中；未清理完前不能删除账号）</Typography.Text></Descriptions.Item>
    </Descriptions>
    <section aria-label="基础资源统计">
      <Typography.Title level={4} style={{ marginTop: 0 }}>基础资源统计</Typography.Title>
      <div style={{ display: 'grid', gridTemplateColumns: mobile ? 'minmax(0, 1fr)' : 'repeat(2, minmax(0, 1fr))', gap: 12 }}>
        {([{ key: 'active', label: '正常作品', href: `/admin/projects?owner_user_id=${user.id}` }, { key: 'recycled', label: '回收站', href: `/admin/recycle-bin?owner_user_id=${user.id}` }] as const).map(({ key, label, href }) => <Card key={key} size="small" title={label} extra={<Link to={href}>查看全部</Link>} styles={{ body: { padding: 12 } }}>
          <Descriptions size="small" layout="vertical" column={3}>
            <Descriptions.Item label="作品数">{user.usage[key].project_count.toLocaleString('zh-CN')}</Descriptions.Item>
            <Descriptions.Item label="章节数">{user.usage[key].chapter_count.toLocaleString('zh-CN')}</Descriptions.Item>
            <Descriptions.Item label="正文总字数">{user.usage[key].total_words.toLocaleString('zh-CN')}</Descriptions.Item>
          </Descriptions>
        </Card>)}
      </div>
      <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>统计覆盖全部所属作品，不是列表页小计。章节含空章，字数仅计当前正文；回收站包含到期待清理作品。</Typography.Paragraph>
      {user.usage.deleting.project_count > 0 && <Alert showIcon type="warning" style={{ marginTop: 12 }} message={`${user.usage.deleting.project_count} 部作品正在删除处理`} description={`${user.usage.deleting.chapter_count} 章 / ${user.usage.deleting.total_words.toLocaleString('zh-CN')} 字暂不计入正常作品或回收站，仍计入全部作品数。请稍后刷新核实。`} />}
    </section>
  </Space>
}

interface ResultsProps { items: AdminProject[]; mobile: boolean; loading: boolean }

/** 所属正常作品只有管理入口；回收站必须使用专属目录，不伪装成可编辑项目。 */
export function AdminUserProjectResults({ items, mobile, loading }: ResultsProps) {
  const empty = <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="该用户暂无正常作品" />
  const status = (project: AdminProject) => <Tag color={PROJECT_STATUS[project.status].color}>{PROJECT_STATUS[project.status].label}</Tag>
  const columns: TableColumnsType<AdminProject> = [
    { title: '作品名称', key: 'title', width: 240, render: (_, project) => <Link to={`/projects/${project.id}/overview`}>{project.title || '未命名项目'}</Link> },
    { title: '状态', key: 'status', width: 110, render: (_, project) => status(project) },
    { title: '章节数', dataIndex: 'chapter_count', width: 95 },
    { title: '正文总字数', dataIndex: 'total_words', width: 130, render: (value: number) => value.toLocaleString('zh-CN') },
    { title: '更新时间', dataIndex: 'updated_at', width: 170, render: (value: string) => formatTime(value) },
    { title: '操作', key: 'action', width: 80, render: (_, project) => <Link to={`/projects/${project.id}/overview`}>管理</Link> },
  ]
  if (!mobile) return <Table<AdminProject> rowKey="id" columns={columns} dataSource={items} loading={loading} pagination={false} scroll={{ x: 825 }} locale={{ emptyText: empty }} />
  return <List loading={loading} dataSource={items} locale={{ emptyText: empty }} renderItem={(project) => <List.Item key={project.id} style={{ display: 'block', overflowWrap: 'anywhere' }}>
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 8 }}><Link to={`/projects/${project.id}/overview`}>{project.title || '未命名项目'}</Link>{status(project)}</div>
    <Typography.Paragraph style={{ margin: '8px 0 4px' }}>{project.chapter_count} 章 · {project.total_words.toLocaleString('zh-CN')} 字</Typography.Paragraph>
    <Typography.Text type="secondary">更新于 {formatTime(project.updated_at)}</Typography.Text>
  </List.Item>} />
}

function UserProjects({ userId }: { userId: number }) {
  const mobile = useIsMobile()
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const loader = useCallback(() => adminApi.listProjects({ owner_user_id: userId, page, page_size: pageSize }), [userId, page, pageSize])
  const { data, loading, error, reload } = useLoader(loader)
  return <Card title="所属正常作品" extra={<Link to={`/admin/projects?owner_user_id=${userId}`}>打开作品管理</Link>} styles={{ body: { padding: mobile ? 12 : 16, minWidth: 0 } }}>
    {error && <Alert showIcon type="error" message={error} action={<Button size="small" onClick={reload}>重试作品列表</Button>} style={{ marginBottom: 12 }} />}
    <AdminUserProjectResults items={data?.items ?? []} mobile={mobile} loading={loading} />
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12, marginTop: 16 }}>
      <Typography.Text type="secondary">共 {data?.total ?? 0} 部正常作品</Typography.Text>
      <Pagination current={page} pageSize={pageSize} total={data?.total ?? 0} simple={mobile} showSizeChanger={!mobile} pageSizeOptions={[20, 50, 100]} onChange={(next, size) => { setPage(size !== pageSize ? 1 : next); setPageSize(size); reload() }} />
    </div>
  </Card>
}

interface ContentProps extends SummaryProps { loading: boolean; refreshVersion: number; onRefresh: () => void; onDeleted: () => void }

function UserContent({ user, actorId, mobile, loading, refreshVersion, onRefresh, onDeleted }: ContentProps) {
  const { state, controller } = useAdminUserActions({ actorId, onChanged: onRefresh, onDeleted })
  return <Space direction="vertical" size={20} style={{ width: '100%', minWidth: 0 }}>
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
      <Typography.Title level={3} style={{ margin: 0, overflowWrap: 'anywhere' }}>{user.username} 的用户详情</Typography.Title>
      <AdminUserActionButtons user={user} actorId={actorId} disabled={loading || state.target !== null} onAction={controller.open} />
    </div>
    <AdminUserSummary user={user} actorId={actorId} mobile={mobile} />
    <UserProjects key={`${user.id}:${refreshVersion}`} userId={user.id} />
    <AdminUserActionDialog state={state} controller={controller} />
  </Space>
}

function Details({ actorId, targetId }: { actorId: number; targetId: number }) {
  const mobile = useIsMobile()
  const navigate = useNavigate()
  const [refreshVersion, setRefreshVersion] = useState(0)
  const loader = useCallback(async () => {
    try { return { user: await adminApi.getUser(targetId), failure: null } }
    catch (err) { return { user: null, failure: { status: err instanceof ApiError ? err.status : 0, message: errorMessage(err) } } }
  }, [targetId])
  const { data, loading, error, reload } = useLoader(loader)
  const refresh = useCallback(() => { reload(); setRefreshVersion((value) => value + 1) }, [reload])
  const deleted = useCallback(() => navigate('/admin/users', { replace: true }), [navigate])
  const failure = data?.failure
  return <div style={{ maxWidth: 1300, minWidth: 0, margin: '0 auto', padding: mobile ? 12 : 24 }}>
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12, marginBottom: 20 }}>
      <Link to="/admin/users"><ArrowLeftOutlined /> 返回用户列表</Link>
      <Button icon={<ReloadOutlined />} loading={loading} onClick={refresh}>刷新详情</Button>
    </div>
    {error && <Alert showIcon type="error" message={error} action={<Button onClick={refresh}>重试</Button>} />}
    {data?.user ? <UserContent user={data.user} actorId={actorId} mobile={mobile} loading={loading} refreshVersion={refreshVersion} onRefresh={refresh} onDeleted={deleted} /> : failure ? failure.status === 404 ? <Result status="404" title="用户不存在" subTitle="该账号可能已被删除，请返回用户列表核实。" /> : <Alert showIcon type="error" message={failure.message} action={<Button onClick={refresh}>重试详情</Button>} /> : loading ? <Skeleton active paragraph={{ rows: 9 }} /> : null}
  </div>
}

/** actor 与目标共同决定页面实例；切目标/切账号不复用旧数据或危险操作。 */
export default function AdminUserDetailsPage() {
  const user = useAuthStore((state) => state.user)
  const { userId } = useParams()
  const targetId = parseAdminUserId(userId)
  if (!user?.is_admin) return <Result status="403" title="需要管理员权限" />
  if (targetId === null) return <Result status="404" title="无效的用户 ID" extra={<Link to="/admin/users">返回用户列表</Link>} />
  return <Details key={`${user.id}:${targetId}`} actorId={user.id} targetId={targetId} />
}
