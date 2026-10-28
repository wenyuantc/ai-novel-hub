import { Alert, App, Button, Card, Popconfirm, Space, Tag, Typography, theme } from 'antd'
import { useCallback, useState } from 'react'
import { errorMessage } from '../../api/client'
import { isOpenTask, taskApi, TASK_STATUS } from '../../api/tasks'
import type { AgentChange } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { useTasksStore } from '../../stores/tasks'
import { AGENT_CHANGE_STATUS, agentValue, canUndoChange, changeTaskId } from '../../utils/agent'
import TextDiff from '../TextDiff'
import { TaskOutputModal } from '../TaskPanel'

interface Props {
  change: AgentChange
  busy?: boolean
  confirmationReady?: boolean
  onAction: (change: AgentChange, action: 'confirm' | 'reject' | 'undo') => void
}
export default function AgentChangeCard({ change, busy, confirmationReady = true, onAction }: Props) {
  const { token } = theme.useToken()
  const state = AGENT_CHANGE_STATUS[change.status]
  const taskId = changeTaskId(change)
  return <Card size="small" title={<span style={{ whiteSpace: 'normal' }}>{change.preview.title}</span>} extra={<Tag color={state.color}>{state.label}</Tag>} styles={{ body: { padding: 12 } }}>
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      {change.preview.warnings.map((warning, i) => <Alert key={i} type="warning" showIcon message={warning} />)}
      {change.kind === 'task' && <Typography.Text type="secondary">启动后会消耗 AI 调用，可在任务列表查看进度或取消。</Typography.Text>}
      {change.preview.diff.length > 0 && <div style={{ overflowX: 'auto', maxWidth: '100%' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', tableLayout: 'fixed', fontSize: 13 }}>
          <thead><tr><th scope="col" style={{ width: '24%', textAlign: 'start' }}>字段</th><th scope="col">修改前</th><th scope="col">修改后</th></tr></thead>
          <tbody>{change.preview.diff.map((d, index) => <tr key={`${d.field}-${index}`}>
            <th scope="row" style={{ textAlign: 'start', verticalAlign: 'top', padding: '8px 4px', borderTop: `1px solid ${token.colorBorderSecondary}`, overflowWrap: 'anywhere' }}>{d.label}</th>
            {[d.before, d.after].map((value, i) => <td key={i} style={{ verticalAlign: 'top', padding: '8px 4px', borderTop: `1px solid ${token.colorBorderSecondary}`, overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}>{agentValue(value)}</td>)}
          </tr>)}</tbody>
        </table>
      </div>}
      {change.preview.diff.filter((d) => typeof d.before === 'string' && typeof d.after === 'string' && (change.kind === 'text' || d.before.length + d.after.length > 500)).map((d) => <details key={d.field}><summary>{d.label} · 逐字对比</summary><TextDiff a={String(d.before)} b={String(d.after)} maxHeight={240} /></details>)}
      {change.error && <Alert type="error" showIcon message={change.error} />}
      <Space wrap>
        {change.status === 'proposed' && <>
          <Button size="small" type="primary" loading={busy} disabled={!confirmationReady} onClick={() => onAction(change, 'confirm')}>确认</Button>
          <Button size="small" disabled={busy || !confirmationReady} onClick={() => onAction(change, 'reject')}>拒绝</Button>
        </>}
        {canUndoChange(change) && <Popconfirm title="撤销这项修改？" description="若数据之后又被修改，将提示冲突并保留当前内容。" onConfirm={() => onAction(change, 'undo')}><Button size="small" loading={busy}>撤销</Button></Popconfirm>}
        {taskId !== null && <AgentTaskActions taskId={taskId} />}
      </Space>
    </Space>
  </Card>
}

function AgentTaskActions({ taskId }: { taskId: number }) {
  const version = useTasksStore((s) => s.version)
  const track = useTasksStore((s) => s.track)
  const { message } = App.useApp()
  const [viewing, setViewing] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const loader = useCallback(() => { void version; return taskApi.get(taskId) }, [taskId, version])
  const { data: task, error, reload } = useLoader(loader)
  const cancel = async () => {
    setCancelling(true)
    try { track(await taskApi.cancel(taskId)); reload(); message.success('已取消任务') }
    catch (err) { message.error(errorMessage(err)); reload() }
    finally { setCancelling(false) }
  }
  return <>
    {task && <Tag color={TASK_STATUS[task.status].color}>{TASK_STATUS[task.status].label}</Tag>}
    <Button size="small" disabled={!task} onClick={() => setViewing(true)}>查看任务</Button>
    {task && isOpenTask(task.status) && <Popconfirm title="取消这个后台任务？" onConfirm={() => cancel()}><Button size="small" loading={cancelling}>取消任务</Button></Popconfirm>}
    {error && <Typography.Text type="secondary">{error}</Typography.Text>}
    {viewing && task && <TaskOutputModal task={task} onClose={() => setViewing(false)} />}
  </>
}
