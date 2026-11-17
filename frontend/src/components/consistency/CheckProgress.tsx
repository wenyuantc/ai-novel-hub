import { Alert, Button, Popconfirm, Progress, Space, Typography } from 'antd'
import { queueLabel, TASK_STATUS } from '../../api/tasks'
import type { Task } from '../../api/types'

interface Props {
  /** 实时订阅中的任务；为 null 表示只知道「有检查在进行」（非发起者，或订阅失败后回退到轮询） */
  task: Task | null
  reconnecting: boolean
  /** 订阅失败的原因（已回退到轮询） */
  lostMessage: string | null
  canCancel: boolean
  cancelling: boolean
  onCancel: () => void
  /** 可重新订阅时提供（发起者本人） */
  onRetrySubscribe?: () => void
  /** 文案对应的任务：一致性检查（默认）或修改影响评估 */
  kind?: 'check' | 'impact'
}

const COPY = {
  check: {
    noun: '检查', title: '一致性检查进行中', cancelTitle: '取消本次检查？',
    cancelNote: '已完成章节的事实台账会保留，重试时从未完成的章节继续。',
    others: '本作品有一致性检查正在运行，结束后本页会自动刷新。', progressLabel: '检查进度',
  },
  impact: {
    noun: '评估', title: '修改影响评估进行中', cancelTitle: '取消本次评估？',
    cancelNote: '尚未保存的评估结果会丢弃；已重新抽取的本章事实台账保留，正文与设定不受影响。',
    others: '本作品有修改影响评估正在运行，结束后本页会自动刷新。', progressLabel: '评估进度',
  },
} as const

/** 进行中的检查：发起者看到阶段与进度并可取消；其他人只看到「检查进行中」。 */
export default function CheckProgress({ task, reconnecting, lostMessage, canCancel, cancelling, onCancel, onRetrySubscribe, kind = 'check' }: Props) {
  const copy = COPY[kind]
  const cancel = canCancel && <Popconfirm title={copy.cancelTitle} description={copy.cancelNote}
    okText={`取消${copy.noun}`} cancelText="不取消" onConfirm={onCancel}>
    <Button danger loading={cancelling}>取消{copy.noun}</Button>
  </Popconfirm>
  if (!task) {
    return <Alert type="info" showIcon role="status" message={`${copy.noun}进行中`}
      description={<Space direction="vertical" size={4}>
        <span>{lostMessage ? `暂时无法接收实时进度，正在定时查询${copy.noun}是否结束。` : copy.others}</span>
        {lostMessage && <Typography.Text type="secondary" style={{ overflowWrap: 'anywhere' }}>{lostMessage}</Typography.Text>}
      </Space>}
      action={<Space wrap>{onRetrySubscribe && lostMessage && <Button onClick={onRetrySubscribe}>重新连接进度</Button>}{cancel}</Space>} />
  }
  const stage = task.status === 'queued' ? queueLabel(task) : task.stage || TASK_STATUS[task.status].label
  return <Alert type="info" showIcon role="status" message={copy.title}
    description={<Space direction="vertical" size={4} style={{ width: '100%' }}>
      <Typography.Text aria-live="polite">{stage}</Typography.Text>
      <Progress percent={Math.max(0, Math.min(100, Math.round(task.progress)))} status="active" aria-label={copy.progressLabel} />
      {reconnecting && <Typography.Text type="warning">连接中断，正在重新连接…</Typography.Text>}
      <Typography.Text type="secondary">可以离开本页，{copy.noun}会在后台继续；也可在任务面板查看。</Typography.Text>
    </Space>}
    action={cancel} />
}
