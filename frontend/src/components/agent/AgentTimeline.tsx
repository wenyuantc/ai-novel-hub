import { Button, Empty, Popconfirm, Space, Spin, Tag, Typography, theme } from 'antd'
import { useEffect, useMemo, useRef } from 'react'
import type { AgentChange, AgentMessage } from '../../api/types'
import { buildAgentTimeline, canUndoChange, type AgentToolStep } from '../../utils/agent'
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
  actingId?: number | null
  confirmationReady: boolean
  onAction: (change: AgentChange, action: 'confirm' | 'reject' | 'undo') => void
  onTurn: (id: number, action: 'confirm-all' | 'reject-all' | 'undo') => void
}
export default function AgentTimeline({ messages, changes, steps, text, running, busy, actingId, confirmationReady, onAction, onTurn, focusMessageId }: Props) {
  const { token } = theme.useToken()
  const scrollRef = useRef<HTMLDivElement>(null)
  const stickRef = useRef(true)
  const located = useRef<number | null>(null)
  const items = useMemo(() => buildAgentTimeline(messages, changes, steps), [messages, changes, steps])
  const turns = [...new Set(changes.map((c) => c.turn_task_id).filter((id): id is number => id !== null))]
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
        {item.type === 'tool' && <details style={{ background: token.colorFillQuaternary, borderRadius: token.borderRadius, padding: 8 }}>
          <summary style={{ cursor: 'pointer', overflowWrap: 'anywhere' }}>{item.step.label} · {item.step.ok === undefined ? '处理中' : item.step.ok ? '完成' : '未完成'}</summary>
          <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 200, overflowY: 'auto', fontSize: 12 }}>{item.step.summary || '正在读取或准备修改…'}</pre>
        </details>}
        {item.type === 'change' && <AgentChangeCard change={item.change} busy={busy} acting={actingId === undefined ? undefined : busy && actingId === item.change.id} confirmationReady={confirmationReady} onAction={onAction} />}
      </div>)}
      {running && <div><Typography.Text type="secondary">项目智能体 <Spin size="small" /></Typography.Text>{text && <AgentMarkdown text={text} />}</div>}
      {turns.map((id) => {
        const pending = changes.filter((c) => c.turn_task_id === id && c.status === 'proposed')
        const undoable = changes.filter((c) => c.turn_task_id === id && canUndoChange(c))
        return <Space key={id} wrap>
          {pending.length > 1 && <>
            <Button size="small" type="primary" disabled={!confirmationReady} loading={busy} onClick={() => onTurn(id, 'confirm-all')}>全部确认（{pending.length}）</Button>
            <Button size="small" disabled={busy || !confirmationReady} onClick={() => onTurn(id, 'reject-all')}>全部拒绝</Button>
          </>}
          {undoable.length > 1 && <Popconfirm title={`撤销这一轮的 ${undoable.length} 项修改？`} description="后台任务需单独取消。任一项冲突时不会撤销任何修改。" onConfirm={() => onTurn(id, 'undo')}><Button size="small" disabled={busy || running}>撤销整轮</Button></Popconfirm>}
        </Space>
      })}
    </Space>
  </div>
}
