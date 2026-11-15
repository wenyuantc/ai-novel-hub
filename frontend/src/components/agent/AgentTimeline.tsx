import { Button, Empty, Popconfirm, Space, Spin, Tag, Typography, theme } from 'antd'
import { useEffect, useMemo, useRef } from 'react'
import { agentChangeIdentity, type AgentTurnAction } from '../../api/agent'
import { hasTaskIdentity, type TaskIdentity } from '../../api/tasks'
import { captureProjectScope, isProjectScopeCurrent, type ProjectScope } from '../../stores/project'
import type { AgentChange, AgentMessage, Task } from '../../api/types'
import { agentToolStepState, buildAgentTimeline, canUndoChange, isAgentTurnOpen, type AgentToolStep } from '../../utils/agent'
import AgentChangeCard from './AgentChangeCard'
import AgentMarkdown from './AgentMarkdown'

interface Props {
  focusMessageId?: number | null
  messages: AgentMessage[]
  changes: AgentChange[]
  steps: AgentToolStep[]
  text: string
  running: boolean
  busy: boolean
  /** 正在处理的提案 id */
  actingKey?: string | null
  confirmationReady: boolean
  /** 当前未结束的轮次（会话详情的 active_task）：其余轮次残留的待确认提案按「轮次已结束」置灰（裁决 26）。 */
  confirmationTask?: Pick<Task, 'id' | 'task_key' | 'status'> | null
  /** 服务端 writable：为 false 时确认 / 拒绝 / 撤销全部不可用（只读会话，裁决 4、7）。 */
  canWrite?: boolean
  onAction: (change: AgentChange, action: 'confirm' | 'reject' | 'undo') => void
  onTurn: (task: TaskIdentity, action: AgentTurnAction, shown: AgentChange[]) => void
}
export default function AgentTimeline({ messages, changes, steps, text, running, busy, actingKey, confirmationReady, confirmationTask, canWrite = true, onAction, onTurn, focusMessageId }: Props) {
  const { token } = theme.useToken()
  const scrollRef = useRef<HTMLDivElement>(null)
  const stickRef = useRef(true)
  const located = useRef<number | null>(null)
  const items = useMemo(() => buildAgentTimeline(messages, changes, steps), [messages, changes, steps])
  const turns = new Map<string, { task: TaskIdentity; shown: AgentChange[] }>()
  for (const change of changes) {
    if (change.turn_task_id === null) continue
    const task = { id: change.turn_task_id, task_key: change.turn_task_key ?? null }
    const key = `${task.id}:${task.task_key ?? ''}`
    const group = turns.get(key) ?? { task, shown: [] }
    group.shown.push(change); turns.set(key, group)
  }
  useEffect(() => {
    if (!focusMessageId || located.current === focusMessageId) return
    stickRef.current = false
    const node = scrollRef.current?.querySelector<HTMLElement>(`[data-message-id="${focusMessageId}"]`)
    if (node) { located.current = focusMessageId; node.scrollIntoView({ block: 'center' }); node.focus({ preventScroll: true }) }
  }, [focusMessageId, items])
  useEffect(() => {
    const el = scrollRef.current
    if (el && stickRef.current) el.scrollTop = el.scrollHeight
  }, [items, text])
  return <div ref={scrollRef} aria-label="智能体对话记录" role="log" aria-live="polite" style={{ overflowY: 'auto', flex: 1, minHeight: 120, padding: 16 }} onScroll={() => {
    const el = scrollRef.current
    if (el) stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }}>
    {items.length === 0 && !running && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<span>从当前项目开始对话<br /><Typography.Text type="secondary">例如：检查未回收的伏笔，或把主角的性格改得更冷静。</Typography.Text></span>} />}
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {items.map((item) => <div key={item.key}>
        {item.type === 'message' && <div data-message-id={item.message.id} tabIndex={focusMessageId === item.message.id ? -1 : undefined} style={{ padding: item.message.role === 'user' ? 12 : 0, background: focusMessageId === item.message.id ? token.colorWarningBg : item.message.role === 'user' ? token.colorFillQuaternary : undefined, borderRadius: token.borderRadius, outline: focusMessageId === item.message.id ? `1px solid ${token.colorWarning}` : undefined }}>
          <Space size={4} wrap>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>{item.message.role === 'user' ? '你' : item.message.role === 'tool' ? '操作记录' : '项目智能体'}</Typography.Text>
            {item.message.role === 'user' && item.message.skill && <Tag style={{ marginInlineEnd: 0 }} title="本轮强制启用的 Skill">Skill：{item.message.skill.name}</Tag>}
          </Space>
          {item.message.role === 'assistant' ? <AgentMarkdown text={item.message.content} /> : <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{item.message.content}</div>}
        </div>}
        {item.type === 'tool' && <ToolStep step={item.step} state={agentToolStepState(item.step, confirmationTask, running && steps.some((s) => s.call_id === item.step.call_id
          && s.turn_task_id === item.step.turn_task_id && s.turn_task_key === item.step.turn_task_key))} />}
        {item.type === 'change' && <AgentChangeCard change={item.change} busy={busy} acting={actingKey === undefined ? undefined : busy && actingKey === item.change.change_key}
          confirmationReady={confirmationReady && confirmationTask?.id === item.change.turn_task_id && !!confirmationTask.task_key
            && confirmationTask.task_key === item.change.turn_task_key}
          turnEnded={item.change.status === 'proposed' && !isAgentTurnOpen(item.change, confirmationTask)} canWrite={canWrite} onAction={onAction} />}
      </div>)}
      {running && <div><Typography.Text type="secondary">项目智能体 <Spin size="small" /></Typography.Text>{text && <AgentMarkdown text={text} />}</div>}
      {[...turns.entries()].map(([key, group]) => <TurnActions key={key} {...group} busy={busy} running={running} canWrite={canWrite}
        ended={!isAgentTurnOpen({ turn_task_id: group.task.id, turn_task_key: group.task.task_key }, confirmationTask)}
        confirmationReady={confirmationReady && confirmationTask?.id === group.task.id && confirmationTask.task_key === group.task.task_key}
        onTurn={onTurn} />)}
    </Space>
  </div>
}

function ToolStep({ step, state }: { step: AgentToolStep; state: { label: string; stopped: boolean } }) {
  const { token } = theme.useToken()
  return <details style={{ background: token.colorFillQuaternary, borderRadius: token.borderRadius, padding: 8 }}>
    <summary style={{ cursor: 'pointer', overflowWrap: 'anywhere' }}>{step.label} · {state.label}</summary>
    <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 200, overflowY: 'auto', fontSize: 12 }}>{step.summary || (state.stopped ? '本轮已结束，此步骤没有返回结果。' : '正在读取或准备修改…')}</pre>
  </details>
}

function TurnActions({ task, shown, busy, running, confirmationReady, canWrite, ended, onTurn }: {
  task: TaskIdentity; shown: AgentChange[]; busy: boolean; running: boolean; confirmationReady: boolean; canWrite: boolean; ended: boolean; onTurn: Props['onTurn']
}) {
  const pending = shown.filter((change) => change.status === 'proposed')
  const undoable = shown.filter(canUndoChange)
  const snapshot = () => ({ task: { ...task }, shown: shown.map((change) => ({ ...change })) })
  const undoSnapshot = useRef<{ value: ReturnType<typeof snapshot>; scope: ProjectScope | null } | null>(null)
  const identified = hasTaskIdentity(task)
  const validPending = identified && pending.every((change) => !!agentChangeIdentity(change))
  const validUndo = identified && undoable.every((change) => !!agentChangeIdentity(change))
  return <Space wrap>
    {pending.length > 1 && !ended && <>
      <Button size="small" type="primary" disabled={!validPending || !confirmationReady} loading={busy} onClick={() => { const captured = snapshot(); onTurn(captured.task, 'confirm-all', captured.shown) }}>全部确认（{pending.length}）</Button>
      <Button size="small" disabled={!validPending || busy || !confirmationReady} onClick={() => { const captured = snapshot(); onTurn(captured.task, 'reject-all', captured.shown) }}>全部拒绝</Button>
    </>}
    {undoable.length > 1 && <Popconfirm disabled={!canWrite} title={`撤销这一轮的 ${undoable.length} 项修改？`} description="后台任务需单独取消。任一项冲突时不会撤销任何修改。"
      onOpenChange={(open) => { if (open) undoSnapshot.current = { value: snapshot(), scope: captureProjectScope(shown[0]?.project_id ?? 0) } }}
      onConfirm={() => { const captured = undoSnapshot.current; if (captured && isProjectScopeCurrent(captured.scope, 'canUseAgent')) onTurn(captured.value.task, 'undo', captured.value.shown) }}><Button size="small" disabled={!canWrite || !validUndo || busy || running}>撤销整轮</Button></Popconfirm>}
  </Space>
}
