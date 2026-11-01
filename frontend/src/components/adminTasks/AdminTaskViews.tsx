import { Alert, Button, Descriptions, Empty, List, Progress, Space, Table, Tag, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import type { AdminTaskBrief, AdminTaskDetail } from '../../api/types'
import { ADMIN_TASK_LIFECYCLE, adminTaskCurrentDuration, adminTaskError, adminTaskKind, adminTaskStatus, adminTaskTitle } from '../../utils/adminTasks'
import { formatTime } from '../../utils/time'

export function AdminTaskStatusTags({ task }: { task: AdminTaskBrief }) {
  const status = adminTaskStatus(task.status)
  return <Space size={[2, 4]} wrap><Tag color={status.color}>{status.label}</Tag>{task.cancellation_pending && <Tag color="processing">取消中</Tag>}</Space>
}
export function AdminTaskProject({ task }: { task: AdminTaskBrief }) {
  return <span className="admin-task-wrap">{task.project_id === null ? '无关联作品' : `${task.project_title ?? '未命名作品'} · #${task.project_id}`}
    {task.project_lifecycle !== 'active' && task.project_lifecycle !== 'none' && <><br /><Tag color={task.project_lifecycle === 'deleting' ? 'warning' : 'default'}>{ADMIN_TASK_LIFECYCLE[task.project_lifecycle]}</Tag></>}
  </span>
}
function TaskProgress({ task }: { task: AdminTaskBrief }) {
  return <div><AdminTaskStatusTags task={task} /><Progress size="small" percent={Math.max(0, Math.min(100, task.progress))} status={task.status === 'failed' ? 'exception' : task.status === 'succeeded' ? 'success' : 'normal'} style={{ marginBottom: 0 }} /></div>
}
export function AdminTaskResults({ items, loading, mobile, onDetail, onCancel, blockedCancelKey, canCancel = true }: {
  items: AdminTaskBrief[]; loading: boolean; mobile: boolean; onDetail: (key: string) => void; onCancel: (task: AdminTaskBrief) => void; blockedCancelKey?: string; canCancel?: boolean
}) {
  const actions = (task: AdminTaskBrief) => <Space size={4} wrap>
    <Button size="small" onClick={() => { if (task.task_key) onDetail(task.task_key) }} disabled={!task.task_key} aria-label={`查看任务 #${task.id} 详情`}>详情</Button>
    {canCancel && task.can_cancel && !task.cancellation_pending && <Button size="small" danger disabled={!task.task_key || task.task_key === blockedCancelKey} onClick={() => onCancel(task)} aria-label={`申请取消任务 #${task.id}`}>{task.task_key === blockedCancelKey ? '核对中' : '取消'}</Button>}
    {!task.task_key && <Typography.Text type="secondary">未记录执行标识</Typography.Text>}
  </Space>
  const empty = <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的现存任务" />
  const columns: TableColumnsType<AdminTaskBrief> = [
    { title: '任务 / 类型', key: 'task', width: 180, render: (_, task) => <span className="admin-task-wrap"><Typography.Text strong>{adminTaskTitle(task)}</Typography.Text></span> },
    { title: '所属用户', key: 'owner', width: 155, render: (_, task) => <span className="admin-task-wrap">{task.owner_username}<br /><Typography.Text type="secondary">用户 #{task.owner_user_id}</Typography.Text></span> },
    { title: '作品', key: 'project', width: 210, render: (_, task) => <AdminTaskProject task={task} /> },
    { title: '状态 / 进度', key: 'status', width: 165, render: (_, task) => <TaskProgress task={task} /> },
    { title: '安全阶段 / 错误', key: 'stage', width: 180, render: (_, task) => <span className="admin-task-wrap">{task.stage || adminTaskStatus(task.status).label}{adminTaskError(task) && <><br /><Typography.Text type="warning">{adminTaskError(task)}</Typography.Text></>}</span> },
    { title: '本轮时长', key: 'duration', width: 190, render: (_, task) => adminTaskCurrentDuration(task) },
    { title: '创建时间', key: 'created', width: 165, render: (_, task) => formatTime(task.created_at, 'MM-DD HH:mm:ss') },
    { title: '操作', key: 'actions', width: 140, fixed: 'right', render: (_, task) => actions(task) },
  ]
  if (!mobile) return <Table<AdminTaskBrief> size="small" rowKey={(task) => task.task_key ?? `legacy-${task.id}`} columns={columns} dataSource={items} loading={loading} pagination={false} scroll={{ x: 1380 }} locale={{ emptyText: empty }} />
  return <List loading={loading} dataSource={items} locale={{ emptyText: empty }} renderItem={(task) => <List.Item className="admin-task-mobile-item">
    <Space direction="vertical" size={8} style={{ width: '100%', minWidth: 0 }}>
      <div className="admin-task-card-heading"><Typography.Text strong>{adminTaskTitle(task)}</Typography.Text><AdminTaskStatusTags task={task} /></div>
      <div className="admin-task-wrap">{task.owner_username} · 用户 #{task.owner_user_id}<br /><AdminTaskProject task={task} /></div>
      <div className="admin-task-wrap">阶段：{task.stage || adminTaskStatus(task.status).label}{adminTaskError(task) && <div><Typography.Text type="warning">{adminTaskError(task)}</Typography.Text></div>}</div>
      <Progress size="small" percent={Math.max(0, Math.min(100, task.progress))} />
      <Typography.Text>{adminTaskCurrentDuration(task)}</Typography.Text>
      <div className="admin-task-card-heading"><Typography.Text type="secondary">创建于 {formatTime(task.created_at, 'MM-DD HH:mm:ss')}</Typography.Text>{actions(task)}</div>
    </Space>
  </List.Item>} />
}

/** 安全元数据专用详情；不复用个人任务结果、输出或流式面板。 */
export function AdminTaskDetailContent({ task }: { task: AdminTaskDetail }) {
  return <Space direction="vertical" size={16} style={{ width: '100%', overflowWrap: 'anywhere' }}>
    <Alert type="info" showIcon message="仅显示安全元数据" description="不提供生成正文、提示词、原始报错、私有插件或配置；也不提供重试、继续、接管或输出读取。" />
    <Descriptions size="small" column={1} bordered>
      <Descriptions.Item label="任务">{adminTaskTitle(task)}</Descriptions.Item>
      <Descriptions.Item label="执行标识">{task.task_key ?? '未记录'}</Descriptions.Item>
      <Descriptions.Item label="类型">{adminTaskKind(task.kind)}</Descriptions.Item>
      <Descriptions.Item label="所属用户">{task.owner_username} · #{task.owner_user_id}</Descriptions.Item>
      <Descriptions.Item label="关联作品"><AdminTaskProject task={task} /></Descriptions.Item>
      <Descriptions.Item label="作品生命周期">{ADMIN_TASK_LIFECYCLE[task.project_lifecycle] ?? '未知'}</Descriptions.Item>
      <Descriptions.Item label="章节编号">{task.chapter_id === null ? '无关联章节' : `#${task.chapter_id}`}</Descriptions.Item>
      <Descriptions.Item label="当前状态"><AdminTaskStatusTags task={task} />{task.status === 'paused' && <div>审核等待，不占执行并发。</div>}</Descriptions.Item>
      <Descriptions.Item label="安全阶段">{task.stage || adminTaskStatus(task.status).label}</Descriptions.Item>
      <Descriptions.Item label="进度"><Progress size="small" percent={Math.max(0, Math.min(100, task.progress))} /></Descriptions.Item>
      <Descriptions.Item label="当前轮次时长">{adminTaskCurrentDuration(task)}</Descriptions.Item>
      <Descriptions.Item label="创建时间">{formatTime(task.created_at, 'YYYY-MM-DD HH:mm:ss [UTC]Z')}</Descriptions.Item>
      <Descriptions.Item label="本轮入队时间">{task.queued_at ? formatTime(task.queued_at, 'YYYY-MM-DD HH:mm:ss [UTC]Z') : '未记录'}</Descriptions.Item>
      <Descriptions.Item label="本轮开始时间">{task.started_at ? formatTime(task.started_at, 'YYYY-MM-DD HH:mm:ss [UTC]Z') : '未记录'}</Descriptions.Item>
      <Descriptions.Item label="结束时间">{task.finished_at ? formatTime(task.finished_at, 'YYYY-MM-DD HH:mm:ss [UTC]Z') : '尚未结束 / 未记录'}</Descriptions.Item>
      {task.cancel_requested_at && <Descriptions.Item label="取消受理时间">{formatTime(task.cancel_requested_at, 'YYYY-MM-DD HH:mm:ss [UTC]Z')}</Descriptions.Item>}
      {adminTaskError(task) && <Descriptions.Item label="安全错误摘要">{adminTaskError(task)}</Descriptions.Item>}
      <Descriptions.Item label="数据更新时间">{formatTime(task.as_of, 'YYYY-MM-DD HH:mm:ss [UTC]Z')}</Descriptions.Item>
    </Descriptions>
    <Typography.Text type="secondary">时长只描述当前排队或运行的一轮，不是历次累计耗时。旧记录缺少可靠起点时显示“未记录”。取消不撤销已提交内容或已执行的效果。</Typography.Text>
  </Space>
}
