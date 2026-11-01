import { can } from '../utils/permissions'
import { useAuthStore } from '../stores/auth'
import { ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Empty, Select, Skeleton, Space, Statistic, Switch, Typography, theme } from 'antd'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import type { AdminDashboardDays, AdminDashboardOverview, AdminDashboardTasks } from '../api/types'
import AutoGrid from '../components/AutoGrid'
import DashboardHistory from '../components/dashboard/DashboardHistory'
import type { DashboardResource } from '../components/dashboard/dashboardController'
import { useAdminDashboard } from '../components/dashboard/useAdminDashboard'
import { StudioPage } from '../components/StudioUI'
import { DASHBOARD_DAYS, dashboardCount, dashboardTime } from '../utils/adminDashboard'
import './AdminDashboardPage.css'

function ResourcePanel<T extends { as_of: string }>({ title, resource, onReload, extra, children }: {
  title: string
  resource: DashboardResource<T>
  onReload: () => void
  extra?: ReactNode
  children: ReactNode
}) {
  return <Card size="small" title={title} className="admin-dashboard-panel" extra={<Space wrap size={8}>
    {extra}<Button size="small" icon={<ReloadOutlined />} loading={resource.loading} onClick={onReload} aria-label={`刷新${title}`}>{resource.error ? '重试' : '刷新'}</Button>
  </Space>}>
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      {resource.error && <Alert type="error" showIcon message={`${title}加载失败`}
        description={`${resource.error}${resource.data ? '。当前保留上次成功值，数据可能已过期。' : '。尚未取得有效数据，不代表数量为零。'}`} />}
      {resource.data && <Typography.Text type="secondary" className="admin-dashboard-updated">
        {resource.loading ? '正在更新；上次成功数据：' : resource.error ? '上次成功数据（可能已过期）：' : '数据更新时间：'}{dashboardTime(resource.data.as_of)}
      </Typography.Text>}
      {resource.data ? children : resource.loading ? <Skeleton active paragraph={{ rows: 2 }} title={false} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未取得有效统计" />}
    </Space>
  </Card>
}

function Overview({ data }: { data: AdminDashboardOverview }) {
  const actor = useAuthStore((state) => state.user)
  return <>
    <AutoGrid minItemWidth={180} gap={20} mobileColumns={2}>
      <div><Statistic title="用户总数" value={data.users} /><Typography.Text type="secondary">所有账号，含管理员与禁用账号</Typography.Text><div>{can(actor, 'users.read') ? <Link to="/admin/users">用户管理</Link> : <span>用户管理</span>}</div></div>
      <div><Statistic title="作品总数（含回收站）" value={data.projects} /><Space wrap>{can(actor, 'projects.read') ? <Link to="/admin/projects">正常作品管理</Link> : <span>正常作品管理</span>}{can(actor, 'recycle_bin.read') ? <Link to="/admin/recycle-bin">回收站</Link> : <span>回收站</span>}</Space></div>
      <div><Statistic title="章节总数（含回收站）" value={data.chapters} /><Typography.Text type="secondary">当前章节记录，不重复计版本</Typography.Text></div>
      <div><Statistic title="正文总字数（含回收站）" value={data.total_words} /><Typography.Text type="secondary">非空白字符，沿用服务端字数</Typography.Text></div>
    </AutoGrid>
    <div className="admin-dashboard-todos">
      <Space wrap><Typography.Text strong>待办</Typography.Text>{can(actor, 'workshop.read') ? <Link to="/workshop?tab=review">工坊待审核投稿：{dashboardCount(data.pending_submissions)}</Link> : <span>工坊待审核投稿：{dashboardCount(data.pending_submissions)}</span>}</Space>
      {can(actor, 'audit.read') ? <Link to="/admin/audit-logs">查看操作日志</Link> : <span>查看操作日志</span>}
    </div>
  </>
}

function Tasks({ data }: { data: AdminDashboardTasks }) {
  const actor = useAuthStore((state) => state.user)
  const { token } = theme.useToken()
  return <>
    <AutoGrid minItemWidth={150} gap={16} mobileColumns={2}>
      <Statistic title="排队中（当前）" value={data.queued} />
      <Statistic title="运行中（当前）" value={data.running} />
      <Statistic title="任务待审核（当前）" value={data.paused} />
      <Statistic title="上线后新建任务" value={data.new_tasks} />
    </AutoGrid>
    <div className="admin-dashboard-task-results">
      <Typography.Text strong>已记录的累计结束结果</Typography.Text>
      <AutoGrid minItemWidth={130} mobileColumns={2} style={{ marginTop: 12 }}>
        <Statistic title="成功" value={data.succeeded} />
        <Statistic title="失败" value={data.failed} />
        <Statistic title="取消" value={data.cancelled} />
        <Statistic title="中断" value={data.interrupted} />
      </AutoGrid>
    </div>
    <div className="admin-dashboard-task-results">
      <Space wrap size={[24, 8]}>
        <Typography.Text>最近 24 小时失败：<strong style={{ color: data.failed_24h ? token.colorError : token.colorText }}>{dashboardCount(data.failed_24h)}</strong></Typography.Text>
        <Typography.Text>最近 24 小时中断：<strong style={{ color: data.interrupted_24h ? token.colorWarning : token.colorText }}>{dashboardCount(data.interrupted_24h)}</strong></Typography.Text>
      </Space>
    </div>
    <Space wrap>{can(actor, 'tasks.read') ? <Link to="/admin/tasks">查看现存任务</Link> : <span>查看现存任务</span>}{can(actor, 'tasks.read') ? <Link to="/admin/tasks?status=queued">查看现存排队任务</Link> : <span>查看现存排队任务</span>}{can(actor, 'tasks.read') ? <Link to="/admin/tasks?status=running">查看现存运行任务</Link> : <span>查看现存运行任务</span>}{can(actor, 'tasks.read') ? <Link to="/admin/tasks?status=paused">查看现存待审核任务</Link> : <span>查看现存待审核任务</span>}{can(actor, 'tasks.read') ? <Link to="/admin/tasks?status=failed">查看现存失败任务</Link> : <span>查看现存失败任务</span>}{can(actor, 'tasks.read') ? <Link to="/admin/tasks?status=interrupted">查看现存中断任务</Link> : <span>查看现存中断任务</span>}</Space>
    <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
      开始跟踪：{dashboardTime(data.tracking_since)}。初始化存量 {dashboardCount(data.baseline_tasks)} 个，不冒充上线后新建量或完整历史。
      累计结果独立保存，不随任务清理或作品彻删减少；未结束即移除 {dashboardCount(data.removed_unfinished)} 个，不计为成功或失败。
      任务监控仅查询现存明细，不能恢复已移除任务；现存列表数量不保证与累计结果或最近 24 小时统计相等。
    </Typography.Paragraph>
  </>
}

/** 管理员全站运营总览；个人创作主页与任务权限保持独立。 */
export default function AdminDashboardPage() {
  const { allowed, state, controller } = useAdminDashboard()
  if (!allowed) return null
  return <div className="admin-dashboard"><StudioPage title="管理员首页" hint="全站规模、待办与任务运行。统计口径与历史采样信息始终可见。"
    action={<Space wrap>
      <Space size={6}><Switch aria-label="每60秒自动刷新" checked={state.autoRefresh} disabled={state.denied} onChange={(checked) => controller.setAutoRefresh(checked)} /><Typography.Text>60 秒自动刷新</Typography.Text></Space>
      <Button icon={<ReloadOutlined />} disabled={state.denied} onClick={controller.refreshAll}>刷新全部</Button>
    </Space>}>
    {state.denied ? <Alert type="error" showIcon message="管理统计访问已停止"
      description="当前会话或管理员权限已失效，已清空页面统计并停止刷新。请重新登录后再试。" action={<Link to="/projects">返回个人作品</Link>} /> :
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <ResourcePanel title="平台规模与待办" resource={state.overview} onReload={() => controller.refresh('overview')}>
          {state.overview.data && <Overview data={state.overview.data} />}
        </ResourcePanel>
        <ResourcePanel title="全站任务统计" resource={state.tasks} onReload={() => controller.refresh('tasks')}>
          {state.tasks.data && <Tasks data={state.tasks.data} />}
        </ResourcePanel>
        <ResourcePanel title="趋势与历史分析" resource={state.history} onReload={() => controller.refresh('history')}
          extra={<Select<AdminDashboardDays> aria-label="历史统计时间范围" value={state.days} options={DASHBOARD_DAYS} onChange={(days) => controller.setDays(days)} style={{ minWidth: 112 }} />}>
          {state.history.data && <DashboardHistory history={state.history.data} />}
        </ResourcePanel>
      </Space>}
  </StudioPage></div>
}
