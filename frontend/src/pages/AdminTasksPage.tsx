import { ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Collapse, Drawer, Form, Input, InputNumber, Pagination, Result, Select, Skeleton, Space, Switch, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { TASK_KIND_LABELS, TASK_STATUS } from '../api/tasks'
import type { AdminTaskListQuery, AdminTaskSort, TaskKind, TaskStatus } from '../api/types'
import AdminTaskCancelModal from '../components/adminTasks/AdminTaskCancelModal'
import { AdminTaskDetailContent, AdminTaskResults } from '../components/adminTasks/AdminTaskViews'
import { useAdminTasks } from '../components/adminTasks/useAdminTasks'
import { StudioPage } from '../components/StudioUI'
import { useIsMobile, useModalWidth } from '../hooks/useIsMobile'
import { useAuthStore } from '../stores/auth'
import { ADMIN_TASK_SORT, ADMIN_TASK_STATUSES, adminTaskFilterQuery, adminTaskInitialQuery, adminTaskLink, adminTaskStatus, isAdminTaskKey } from '../utils/adminTasks'
import type { AdminTaskFilterValues } from '../utils/adminTasks'
import { errorMessage } from '../api/client'
import { formatTime } from '../utils/time'
import './AdminTasksPage.css'

function AdminTasks({ actorId, initialQuery }: { actorId: number; initialQuery: AdminTaskListQuery }) {
  const { state, controller } = useAdminTasks(actorId, initialQuery)
  const mobile = useIsMobile()
  const width = useModalWidth(720)
  const [params, setParams] = useSearchParams()
  const taskKey = params.get('task_key')
  const [form] = Form.useForm<AdminTaskFilterValues>()
  const [filterError, setFilterError] = useState<string | null>(null)
  const invalidKey = taskKey !== null && !isAdminTaskKey(taskKey)
  useEffect(() => {
    if (isAdminTaskKey(taskKey)) controller.openDetail(taskKey)
    else controller.closeDetail()
  }, [controller, taskKey])
  const clearDetailLink = () => {
    controller.closeDetail()
    if (params.has('task_key')) { const next = new URLSearchParams(params); next.delete('task_key'); setParams(next, { replace: true }) }
  }
  const openDetail = (key: string) => { const next = new URLSearchParams(params); next.set('task_key', key); setParams(next) }
  const applyFilters = (values: AdminTaskFilterValues) => {
    try {
      const query = adminTaskFilterQuery(values)
      clearDetailLink()
      controller.setQuery({ ...query, sort: state.query.sort, page_size: state.query.page_size })
      setFilterError(null)
    } catch (err) { setFilterError(errorMessage(err)) }
  }
  const reset = () => {
    form.resetFields()
    form.setFieldsValue({ q: undefined, owner_user_id: undefined, project_id: undefined, task_id: undefined, kind: undefined, status: [], fromDate: undefined, toDate: undefined })
    clearDetailLink()
    controller.setQuery({})
    setFilterError(null)
  }
  const ownerOptions = (state.owners.data?.items ?? []).map((owner) => ({ value: owner.id, label: `${owner.username} · #${owner.id}` }))
  const selectedOwner = Form.useWatch('owner_user_id', form) ?? state.query.owner_user_id
  if (selectedOwner && !ownerOptions.some((owner) => owner.value === selectedOwner)) ownerOptions.unshift({ value: selectedOwner, label: `用户 #${selectedOwner}` })
  const filters = <Form form={form} initialValues={initialQuery} layout="vertical" onFinish={applyFilters}>
    <div className="admin-task-filters">
      <Form.Item name="q" label="用户名 / 作品标题"><Input allowClear placeholder="字面量搜索，不搜索任务原文" maxLength={200} /></Form.Item>
      <Form.Item name="owner_user_id" label="所属用户"><Select<number> aria-label="按所属用户筛选" allowClear showSearch filterOption={false} placeholder="选择或搜索用户" options={ownerOptions} loading={state.owners.loading} onFocus={() => controller.searchOwners()} onSearch={controller.searchOwners} notFoundContent={state.owners.error ?? (state.owners.loading ? '正在加载…' : '没有匹配用户')} /></Form.Item>
      <Form.Item name="project_id" label="作品 ID"><InputNumber min={1} max={Number.MAX_SAFE_INTEGER} precision={0} placeholder="精确作品编号" /></Form.Item>
      <Form.Item name="task_id" label="任务 ID"><InputNumber min={1} max={Number.MAX_SAFE_INTEGER} precision={0} placeholder="仅筛选，不作为操作身份" /></Form.Item>
      <Form.Item name="kind" label="任务类型"><Select<TaskKind> allowClear showSearch optionFilterProp="label" placeholder="全部类型" options={(Object.keys(TASK_KIND_LABELS) as TaskKind[]).map((value) => ({ value, label: TASK_KIND_LABELS[value] }))} /></Form.Item>
      <Form.Item name="status" label="任务状态（可多选）"><Select<TaskStatus[]> mode="multiple" allowClear maxTagCount="responsive" placeholder="全部状态" options={(Object.keys(TASK_STATUS) as TaskStatus[]).map((value) => ({ value, label: TASK_STATUS[value].label }))} /></Form.Item>
      <Form.Item name="fromDate" label="创建开始日（本地，含）"><Input type="date" /></Form.Item>
      <Form.Item name="toDate" label="创建结束日（本地，含）"><Input type="date" /></Form.Item>
    </div>
    <Space wrap><Button type="primary" htmlType="submit" disabled={state.denied}>查询</Button><Button onClick={reset}>重置筛选</Button></Space>
    <Typography.Paragraph type="secondary" style={{ margin: '12px 0 0' }}>日期按浏览器本地日历选择，转换为 UTC 半开区间；所有时间按本地时区显示。用户名和作品标题中的 %、_ 按字面量搜索。</Typography.Paragraph>
  </Form>
  const data = state.list.data
  const sortSelect = <Select<AdminTaskSort> aria-label="任务排序" value={state.query.sort} style={{ width: mobile ? '100%' : 188, maxWidth: '100%' }} options={(Object.keys(ADMIN_TASK_SORT) as AdminTaskSort[]).map((value) => ({ value, label: ADMIN_TASK_SORT[value] }))} onChange={(sort) => { clearDetailLink(); controller.setQuery({ ...state.query, sort, page: 1 }) }} />
  return <div className="admin-tasks"><StudioPage title="全站任务监控" hint="仅查看现存任务的安全元数据，按当前筛选定位任务；不读取其他用户的生成内容或私有配置。"
    action={<Space wrap>
      <Space size={6}><Switch aria-label="每5秒自动刷新任务" checked={state.autoRefresh} disabled={state.denied} onChange={(checked) => controller.setAutoRefresh(checked)} /><Typography.Text>5 秒自动刷新</Typography.Text></Space>
      <Button icon={<ReloadOutlined />} loading={state.list.loading} disabled={state.denied} onClick={controller.refreshAll}>刷新</Button>
    </Space>}>
    {state.denied ? <Alert type="error" showIcon message="任务监控访问已停止" description="当前会话或管理员权限已失效，已清空列表、详情和取消确认并停止刷新。请重新登录后再试。" action={<Link to="/projects">返回个人作品</Link>} /> : <Space direction="vertical" size={16} style={{ width: '100%', minWidth: 0 }}>
      <Typography.Text type="secondary">本页仅包含尚未移除的明细，清理后不再显示。<Link to="/admin/dashboard">管理员首页</Link>的累计统计独立保留，两者数量不保证一致。页面隐藏时暂停自动刷新，恢复可见后更新。</Typography.Text>
      {invalidKey && <Alert type="warning" showIcon message="任务执行标识无效" description="详情必须使用 UUID 定位；不会按整数编号读取或处置其他任务。" />}
      {state.notice && <Alert showIcon type={state.notice.type} message={`${state.notice.title}：${state.notice.text}`}
        description={<span className="admin-task-wrap">{state.notice.operationId && <>受理标识：{state.notice.operationId}。 </>}{state.notice.auditLogId != null && <>审计日志 #{state.notice.auditLogId}。 </>}<Link to="/admin/audit-logs">查看管理操作日志</Link></span>} />}
      <Card size="small" title="组合筛选">
        {mobile ? <Collapse ghost items={[{ key: 'filters', label: '展开 / 收起筛选条件', children: filters }]} /> : filters}
        {filterError && <Alert type="error" showIcon message={filterError} style={{ marginTop: 12 }} />}
      </Card>
      <Card size="small" title="当前筛选下的现存任务" extra={!mobile && sortSelect}>
        <Space direction="vertical" size={16} style={{ width: '100%', minWidth: 0 }}>
          {mobile && <div><Typography.Text>排序方式</Typography.Text>{sortSelect}</div>}
          {state.list.error && <Alert type="error" showIcon message="现存任务加载失败" description={`${state.list.error}。${data ? '当前保留上次成功数据，可能已过期。' : '尚未取得有效明细，不代表数量为零。'}`} action={<Button size="small" disabled={state.list.loading} onClick={controller.refreshList}>重试</Button>} />}
          {data ? <>
            <div className="admin-task-summary" aria-label="当前筛选所有页的状态计数"><Typography.Text strong>共 {data.total} 个</Typography.Text>{ADMIN_TASK_STATUSES.filter((status) => status !== 'unknown' || data.counts.unknown > 0).map((status) => <Typography.Text key={status}>{adminTaskStatus(status).label} {data.counts[status]}</Typography.Text>)}</div>
            <Typography.Text type="secondary">{state.list.error ? '上次成功数据（可能已过期）：' : state.list.loading ? '更新中，上次成功数据：' : '数据更新时间：'}{formatTime(data.as_of, 'YYYY-MM-DD HH:mm:ss [UTC]Z')}。计数覆盖当前筛选所有页，不是历史累计；各次刷新不是冻结快照。</Typography.Text>
            <AdminTaskResults items={data.items} loading={state.list.loading} mobile={mobile} onDetail={openDetail} onCancel={controller.openCancellation} blockedCancelKey={state.notice?.checking ? state.notice.taskKey : undefined} />
            <div className="admin-task-pagination">
              <Typography.Text type="secondary">时长仅计本轮，起点缺失显示“未记录”；待审核不占执行并发。时长排序优先对应状态，未知时间置后。</Typography.Text>
              <Space wrap>{mobile && <Select<number> aria-label="每页任务数" value={state.query.page_size} options={[20, 50, 100].map((value) => ({ value, label: `${value} 条/页` }))} onChange={(size) => { clearDetailLink(); controller.setPage(1, size) }} />}<Pagination current={state.query.page} pageSize={state.query.page_size} total={data.total} simple={mobile} showSizeChanger={!mobile} pageSizeOptions={[20, 50, 100]} onChange={(page, size) => { clearDetailLink(); controller.setPage(page, size) }} /></Space>
            </div>
          </> : state.list.loading ? <Skeleton active paragraph={{ rows: 4 }} /> : <Typography.Text type="secondary">尚未取得有效任务明细。</Typography.Text>}
        </Space>
      </Card>
      <Drawer open={state.detailKey !== null} title="任务安全详情" width={width} onClose={clearDetailLink} destroyOnHidden extra={<Button size="small" loading={state.detail.loading} onClick={controller.refreshDetail}>刷新详情</Button>}>
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          {state.detail.error && <Alert type="error" showIcon message={state.detail.error} description={state.detail.data ? '当前保留上次成功详情，数据可能已过期。' : undefined} action={<Button size="small" onClick={controller.refreshDetail}>重试</Button>} />}
          {state.detail.loading && !state.detail.data && <Skeleton active />}
          {state.detail.data && <>
            {state.detail.data.task_key && <Link to={adminTaskLink(state.detail.data.task_key)}>此任务的 UUID 定位链接</Link>}
            <AdminTaskDetailContent task={state.detail.data} />
            {state.detail.data.can_cancel && !state.detail.data.cancellation_pending && <Button danger disabled={state.notice?.checking && state.notice.taskKey === state.detail.data.task_key} onClick={() => { if (state.detail.data) controller.openCancellation(state.detail.data) }}>申请取消此任务</Button>}
          </>}
        </Space>
      </Drawer>
      {state.cancelTarget && <AdminTaskCancelModal key={state.cancelTarget.task_key} task={state.cancelTarget} loading={state.cancelLoading} submitting={state.submitting} error={state.cancelError} onClose={controller.closeCancellation} onSubmit={(reason) => controller.submitCancellation(reason)} />}
    </Space>}
  </StudioPage></div>
}

/** 按真实 actor 重建全部页面数据，管理员降权立即卸载安全明细与确认框。 */
export default function AdminTasksPage() {
  const user = useAuthStore((state) => state.user)
  const [params] = useSearchParams()
  return user?.is_admin ? <AdminTasks key={user.id} actorId={user.id} initialQuery={adminTaskInitialQuery(params)} /> : <Result status="403" title="需要管理员权限" />
}
