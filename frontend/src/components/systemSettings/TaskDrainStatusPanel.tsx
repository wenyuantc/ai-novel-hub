import { ReloadOutlined } from '@ant-design/icons'
import { Button, Space, Typography } from 'antd'
import { Link } from 'react-router-dom'
import { formatTime } from '../../utils/time'
import type { SystemSettingsState } from './systemSettingsController'

const fields = [
  ['queued', '排队中'],
  ['running', '运行中'],
  ['paused', '待审核'],
  ['finalizing', '收尾中'],
  ['active_backups', '活动备份'],
] as const

export function TaskDrainStatusPanel({ status, canReadTasks, onRefresh }: {
  status: SystemSettingsState['drain']
  canReadTasks: boolean
  onRefresh: () => void
}) {
  const { data, error, loading, stale } = status
  let summary: string | undefined
  if (error) summary = '排空状态读取失败'
  else if (stale) summary = '排空状态已过期'
  else if (data) summary = data.enabled ? fields.every(([key]) => data[key] === 0) ? '已排空' : '正在排空' : '正在受理新任务'

  return <section className="system-setting-group">
    <h2>任务排空状态</h2>
    <div className="task-drain-details">
      <div className="task-drain-toolbar">
        <Typography.Text role="status" strong type={stale ? 'warning' : undefined}>{summary}</Typography.Text>
        <Space wrap>
          <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={onRefresh}>刷新排空状态</Button>
          {canReadTasks && <Link to="/admin/tasks">全站任务监控</Link>}
        </Space>
      </div>
      {error && <p className="system-setting-baseline">{error}</p>}
      {!data && !error && <p className="system-setting-baseline">{loading ? '正在读取排空状态…' : '尚未读取排空状态'}</p>}
      {data && <>
        <p className="system-setting-baseline">
          {stale ? '最近一次成功取样' : '取样时间'} <time dateTime={data.sampled_at}>{formatTime(data.sampled_at, 'YYYY-MM-DD HH:mm:ss')}</time>
        </p>
        <dl className="task-drain-counts">
          {fields.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{data[key]}</dd></div>)}
        </dl>
        {data.paused > 0 && <p className="system-setting-baseline">待审核任务需要有权限的用户提前结束或取消。恢复受理后也可继续。</p>}
        <h3>存活进程</h3>
        {data.workers.length === 0 ? <p className="system-setting-baseline">取样时没有存活进程</p> :
          <ul className="task-drain-workers">
            {data.workers.map((worker) => <li key={`${worker.hostname}:${worker.pid}:${worker.started_at}`}>
              <strong>{worker.hostname}（PID {worker.pid}）</strong>
              <span>启动于 <time dateTime={worker.started_at}>{formatTime(worker.started_at, 'YYYY-MM-DD HH:mm:ss')}</time></span>
              <span>最近心跳 <time dateTime={worker.heartbeat_at}>{formatTime(worker.heartbeat_at, 'YYYY-MM-DD HH:mm:ss')}</time></span>
            </li>)}
          </ul>}
      </>}
    </div>
  </section>
}
