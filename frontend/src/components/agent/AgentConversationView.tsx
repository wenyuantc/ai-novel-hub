import { ReloadOutlined, SendOutlined, StopOutlined } from '@ant-design/icons'
import { Alert, AutoComplete, Button, Input, Popconfirm, Select, Skeleton, Space, Switch, Typography, theme } from 'antd'
import { useCallback, useMemo, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { mcpApi } from '../../api/mcp'
import { canRetryTask } from '../../api/tasks'
import { useAgentPageContext } from '../../hooks/useAgentPageContext'
import { useLoader } from '../../hooks/useLoader'
import { useAuthStore } from '../../stores/auth'
import { useSkills } from '../../stores/skills'
import { matchTrigger, triggerOptions } from '../../utils/skills'
import AIConfigSelect from '../AIConfigSelect'
import SkillSelect from '../skills/SkillSelect'
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
  // 本轮强制启用的 Skill（选择器优先于 /触发词）；发送后清空
  const [skillId, setSkillId] = useState<number | null>(null)
  const { skills } = useSkills()
  const mcpEnabled = useAuthStore((s) => s.mcpEnabled)
  const suggestions = useMemo(() => triggerOptions(input, skills), [input, skills])
  const triggered = useMemo(() => (skillId === null ? matchTrigger(input, skills) : null), [input, skills, skillId])
  const { detail, stream } = agent
  const lastTask = agent.lastTask
  const openTask = detail?.active_task
  const blocked = agent.busy || !!openTask || stream.running
  const send = async () => {
    if (blocked || !input.trim() || Array.from(input).length > 8000) return
    if (await agent.send({ content: input.trim(), page_context: context, ai_config_id: configId, skill_id: skillId })) { setInput(''); setSkillId(null) }
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
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, margin: '4px 0 0' }}>每轮首次不超过 3 项小修改可直接执行；删除、后续修改、启动任务和外部插件调用仍需确认。</Typography.Paragraph>
      {mcpEnabled && <AgentPluginSelect value={detail.mcp_plugin_ids ?? []} disabled={!!openTask || agent.busy} onChange={(mcp_plugin_ids) => void agent.update({ mcp_plugin_ids })} />}
    </div>
    {(agent.error || stream.error) && <Alert type="error" showIcon message={agent.error ?? stream.error} action={<Button size="small" onClick={agent.refresh}>刷新</Button>} />}
    {!openTask && !stream.error && lastTask?.error && <Alert type="warning" showIcon message={lastTask.error} />}
    {stream.reconnecting && <Alert type="warning" showIcon message="连接中断，正在恢复对话…后台任务仍在继续。" />}
    {openTask && <div role="status" style={{ padding: '8px 16px' }}><Space wrap>
      <Typography.Text type="secondary">{openTask.status === 'paused' ? '请确认或拒绝修改，随后继续对话' : stream.task?.stage || openTask.stage || '正在思考…'}</Typography.Text>
      <Popconfirm title="停止这一轮对话？" description="已经执行的修改会保留，可逐项撤销。" onConfirm={() => agent.cancel()}><Button size="small" icon={<StopOutlined />} loading={agent.busy}>停止</Button></Popconfirm>
    </Space></div>}
    {lastTask && canRetryTask(lastTask.status) && !openTask && <div style={{ padding: '8px 16px' }}><Button size="small" loading={agent.busy} onClick={() => void agent.retry(lastTask)}>重试本轮</Button></div>}
    <AgentTimeline messages={detail.messages} changes={detail.changes} steps={agent.steps} text={liveText} running={stream.running} busy={agent.busy} actingId={agent.actingId} confirmationReady={openTask?.status === 'paused'} onAction={(item, action) => void agent.change(item, action)} onTurn={(id, action) => void agent.turn(id, action)} />
    <div style={{ padding: 16, borderTop: `1px solid ${token.colorBorderSecondary}` }}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>{contextLabel}{context.selection ? ` · 已附带选中的 ${Array.from(context.selection).length} 字` : ''}</Typography.Paragraph>
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        <AIConfigSelect value={configId} onChange={setConfigId} disabled={blocked} />
        <SkillSelect aria-label="本轮启用的 Skill" value={skillId} onChange={setSkillId} disabled={blocked} placeholder="Skill（可选，或输入 / 选择触发词）" />
        <AutoComplete
          value={input}
          onChange={(value: string) => setInput(value.slice(0, 8000))}
          options={suggestions.map((o) => ({ value: o.value, label: <span style={{ overflowWrap: 'anywhere' }}>/{o.trigger}<Typography.Text type="secondary"> · {o.skill.name}</Typography.Text></span> }))}
          style={{ width: '100%' }}
        >
          <Input.TextArea aria-label="发给项目智能体的消息" placeholder="告诉我你想了解或修改什么…（输入 / 选择 Skill 触发词）" autoSize={{ minRows: 2, maxRows: 6 }} onKeyDown={(e) => {
            // 补全列表展开时 Enter 用于选择触发词
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && suggestions.length === 0) { e.preventDefault(); void send() }
          }} />
        </AutoComplete>
        {(skillId !== null || triggered) && <Typography.Text type="secondary" style={{ fontSize: 12 }}>本轮将启用 Skill「{skillId !== null ? skills.find((s) => s.id === skillId)?.name ?? '已选择' : triggered?.name}」</Typography.Text>}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}><Typography.Text type="secondary" style={{ fontSize: 12 }}>Enter 发送 · Shift + Enter 换行</Typography.Text><Button type="primary" icon={<SendOutlined />} disabled={blocked || !input.trim()} loading={agent.busy} onClick={() => void send()}>发送</Button></div>
      </Space>
    </div>
  </>
}

/** 本会话启用的 MCP 插件（多选）；列表读取失败（如部署关闭 MCP）时不显示。 */
function AgentPluginSelect({ value, disabled, onChange }: { value: number[]; disabled: boolean; onChange: (ids: number[]) => void }) {
  const loader = useCallback(() => mcpApi.list(), [])
  const { data: plugins, loading, error } = useLoader(loader)
  if (error || (plugins && plugins.length === 0 && value.length === 0)) return null
  const known = new Set((plugins ?? []).map((p) => p.id))
  return <div style={{ marginTop: 8 }}>
    <Select<number[]>
      mode="multiple"
      aria-label="本会话启用的 MCP 插件"
      placeholder="启用 MCP 插件（可选）"
      value={value.filter((id) => known.has(id) || loading)}
      loading={loading}
      disabled={disabled}
      onChange={onChange}
      options={(plugins ?? []).map((p) => ({ value: p.id, label: p.enabled ? p.name : `${p.name}（已停用）`, disabled: !p.enabled && !value.includes(p.id) }))}
      maxTagCount="responsive"
      style={{ width: '100%' }}
    />
    <Typography.Text type="secondary" style={{ fontSize: 12 }}>插件工具的调用默认需要确认，且无法撤销。</Typography.Text>
  </div>
}
