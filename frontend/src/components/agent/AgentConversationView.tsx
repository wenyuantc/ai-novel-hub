import { ReloadOutlined, SendOutlined, StopOutlined } from '@ant-design/icons'
import { Alert, Button, Input, Popconfirm, Skeleton, Space, Switch, Typography, theme } from 'antd'
import { useState } from 'react'
import { useLocation } from 'react-router-dom'
import { canRetryTask } from '../../api/tasks'
import { useAgentPageContext } from '../../hooks/useAgentPageContext'
import AIConfigSelect from '../AIConfigSelect'
import AgentTimeline from './AgentTimeline'
import { useAgentConversation } from './useAgentConversation'

interface Props { pid: number; cid: number; onUpdated: () => void }
export default function AgentConversationView({ pid, cid, onUpdated }: Props) {
  const { token } = theme.useToken()
  const location = useLocation()
  const context = useAgentPageContext(pid, location.pathname)
  const agent = useAgentConversation(pid, cid, onUpdated)
  const [input, setInput] = useState('')
  const [configId, setConfigId] = useState<number | null>(null)
  const { detail, stream } = agent
  const lastTask = agent.lastTask
  const openTask = detail?.active_task
  const blocked = agent.busy || !!openTask || stream.running
  const send = async () => {
    if (blocked || !input.trim() || Array.from(input).length > 8000) return
    if (await agent.send({ content: input.trim(), page_context: context, ai_config_id: configId })) setInput('')
  }
  if (!detail) return <div style={{ padding: 16 }}>{agent.error ? <Alert type="error" showIcon message={agent.error} action={<Button onClick={agent.refresh}>重试</Button>} /> : <Skeleton active paragraph={{ rows: 6 }} />}</div>
  const persisted = detail.messages.filter((m) => m.role === 'assistant' && m.turn_task_id === stream.task?.id).map((m) => m.content).join('')
  const liveText = persisted && stream.text.startsWith(persisted) ? stream.text.slice(persisted.length) : stream.text
  const contextLabel = context.chapter_id ? `当前章节 #${context.chapter_id}` : context.outline_id ? `当前大纲 #${context.outline_id}` : context.character_id ? `当前角色 #${context.character_id}` : '当前项目页面'
  return <>
    <div style={{ padding: '8px 16px', borderBottom: `1px solid ${token.colorBorderSecondary}` }}>
      <Space wrap size={8}>
        <Switch size="small" aria-label="自动执行小修改" checked={detail.auto_execute} loading={agent.busy} disabled={!!openTask} onChange={(auto_execute) => void agent.update({ auto_execute })} />
        <Typography.Text>自动执行小修改</Typography.Text>
        <Button type="text" size="small" icon={<ReloadOutlined />} aria-label="刷新对话" onClick={agent.refresh} />
      </Space>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, margin: '4px 0 0' }}>每轮首次不超过 3 项小修改可直接执行；删除、后续修改和启动任务仍需确认。</Typography.Paragraph>
    </div>
    {(agent.error || stream.error) && <Alert type="error" showIcon message={agent.error ?? stream.error} action={<Button size="small" onClick={agent.refresh}>刷新</Button>} />}
    {!openTask && !stream.error && lastTask?.error && <Alert type="warning" showIcon message={lastTask.error} />}
    {stream.reconnecting && <Alert type="warning" showIcon message="连接中断，正在恢复对话…后台任务仍在继续。" />}
    {openTask && <div role="status" style={{ padding: '8px 16px' }}><Space wrap>
      <Typography.Text type="secondary">{openTask.status === 'paused' ? '请确认或拒绝修改，随后继续对话' : stream.task?.stage || openTask.stage || '正在思考…'}</Typography.Text>
      <Popconfirm title="停止这一轮对话？" description="已经执行的修改会保留，可逐项撤销。" onConfirm={() => agent.cancel()}><Button size="small" icon={<StopOutlined />} loading={agent.busy}>停止</Button></Popconfirm>
    </Space></div>}
    {lastTask && canRetryTask(lastTask.status) && !openTask && <div style={{ padding: '8px 16px' }}><Button size="small" loading={agent.busy} onClick={() => void agent.retry(lastTask)}>重试本轮</Button></div>}
    <AgentTimeline messages={detail.messages} changes={detail.changes} steps={agent.steps} text={liveText} running={stream.running} busy={agent.busy} confirmationReady={openTask?.status === 'paused'} onAction={(item, action) => void agent.change(item, action)} onTurn={(id, action) => void agent.turn(id, action)} />
    <div style={{ padding: 16, borderTop: `1px solid ${token.colorBorderSecondary}` }}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>{contextLabel}{context.selection ? ` · 已附带选中的 ${Array.from(context.selection).length} 字` : ''}</Typography.Paragraph>
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        <AIConfigSelect value={configId} onChange={setConfigId} disabled={blocked} />
        <Input.TextArea aria-label="发给项目智能体的消息" value={input} onChange={(e) => setInput(e.target.value)} placeholder="告诉我你想了解或修改什么…" maxLength={8000} autoSize={{ minRows: 2, maxRows: 6 }} onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send() }
        }} />
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}><Typography.Text type="secondary" style={{ fontSize: 12 }}>Enter 发送 · Shift + Enter 换行</Typography.Text><Button type="primary" icon={<SendOutlined />} disabled={blocked || !input.trim()} loading={agent.busy} onClick={() => void send()}>发送</Button></div>
      </Space>
    </div>
  </>
}
