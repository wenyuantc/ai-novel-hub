import { Alert, Button, Card, Popconfirm, Space, Tag, Typography, theme } from 'antd'
import { useRef } from 'react'
import { agentChangeIdentity } from '../../api/agent'
import { hasTaskIdentity } from '../../api/tasks'
import type { AgentChange } from '../../api/types'
import { captureProjectScope, isProjectScopeCurrent, type ProjectScope } from '../../stores/project'
import { agentChangeState, agentValue, canUndoChange, externalCall, externalResult } from '../../utils/agent'
import TextDiff from '../TextDiff'

interface Props {
  change: AgentChange
  /** 有操作进行中：禁用本卡片的按钮 */
  busy?: boolean
  /** 正在处理的就是这张卡片：按钮显示加载状态（外部调用在确认请求中同步执行，最长约 30 秒）；未提供时沿用 busy */
  acting?: boolean
  confirmationReady?: boolean
  /** 所属轮次已结束仍为待确认：显示「轮次已结束」并置灰，不再提供确认 / 拒绝（裁决 26）。 */
  turnEnded?: boolean
  /** 服务端 writable=false（只读会话）：撤销也不可用。 */
  canWrite?: boolean
  onAction: (change: AgentChange, action: 'confirm' | 'reject' | 'undo') => void
}
export default function AgentChangeCard({ change, busy, acting, confirmationReady = true, turnEnded = false, canWrite = true, onAction }: Props) {
  const { token } = theme.useToken()
  const state = agentChangeState(change, null, turnEnded)
  const identified = !!agentChangeIdentity(change) && hasTaskIdentity({ id: change.turn_task_id ?? 0, task_key: change.turn_task_key ?? null })
  const undoSnapshot = useRef<{ change: AgentChange; scope: ProjectScope | null } | null>(null)
  const external = externalCall(change)
  const outcome = change.kind === 'external' ? externalResult(change) : null
  const loading = acting ?? busy
  const pre = { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', margin: 0, maxHeight: 240, overflowY: 'auto', fontSize: 12, background: token.colorFillQuaternary, padding: 8, borderRadius: token.borderRadius } as const
  return <Card size="small" style={turnEnded ? { opacity: 0.6 } : undefined} aria-disabled={turnEnded || undefined} title={<span style={{ whiteSpace: 'normal' }}>{change.preview.title}</span>} extra={<Tag color={state.color}>{state.label}</Tag>} styles={{ body: { padding: 12 } }}>
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      {change.preview.warnings.filter((w) => change.kind !== 'external' || !w.includes('无法撤销')).map((warning, i) => <Alert key={i} type="warning" showIcon message={warning} />)}
      {change.kind === 'task' && <Typography.Text type="secondary">启动后会消耗 AI 调用，可在任务列表查看进度或取消。</Typography.Text>}
      {change.kind === 'external' && <Space direction="vertical" size={8} style={{ width: '100%', minWidth: 0 }}>
        <Space wrap size={[4, 4]}>
          <Tag color="orange">外部操作，无法撤销</Tag>
          {external?.read_only_hint === true && <Tag title="插件自己声明的提示，不代表没有副作用">插件声明只读</Tag>}
        </Space>
        <Typography.Text style={{ overflowWrap: 'anywhere' }}>插件：{external?.plugin.name || '（已删除）'}{external?.plugin.slug ? `（${external.plugin.slug}）` : ''}</Typography.Text>
        <Typography.Text style={{ overflowWrap: 'anywhere' }}>工具：<Typography.Text code>{external?.tool ?? change.tool_name}</Typography.Text></Typography.Text>
        <div>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>参数</Typography.Text>
          <pre aria-label="调用参数" style={pre}>{JSON.stringify(external?.arguments ?? change.arguments, null, 2)}</pre>
        </div>
        {outcome && <details open={!outcome.error}><summary>{outcome.error ? '调用失败' : '调用结果'}</summary><pre style={pre}>{outcome.text || '（空结果）'}</pre></details>}
        {change.status === 'proposed' && loading && <Typography.Text type="secondary">正在调用外部插件，最长约 30 秒…</Typography.Text>}
      </Space>}
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
      {change.error && <Alert type={state.label === '已作废' ? 'warning' : 'error'} showIcon message={change.error} />}
      {turnEnded && <Typography.Text type="secondary">所属轮次已结束，此提案不能再确认；如仍需要，请在会话中重新发起。</Typography.Text>}
      {!identified && <Alert type="warning" showIcon message="提案身份或预览版本不可确认，请刷新后重新确认。" />}
      <Space wrap>
        {change.status === 'proposed' && !turnEnded && <>
          <Button size="small" type="primary" loading={loading} disabled={!identified || !confirmationReady || (busy && !loading)} onClick={() => onAction(change, 'confirm')}>{change.kind === 'external' ? '确认调用' : '确认'}</Button>
          <Button size="small" disabled={!identified || busy || !confirmationReady} onClick={() => onAction(change, 'reject')}>拒绝</Button>
        </>}
        {canUndoChange(change) && <Popconfirm disabled={!canWrite} title="撤销这项修改？" description="若数据之后又被修改，将提示冲突并保留当前内容。"
          onOpenChange={(open) => { if (open) undoSnapshot.current = { change: { ...change }, scope: captureProjectScope(change.project_id) } }}
          onConfirm={() => { const captured = undoSnapshot.current; if (captured && isProjectScopeCurrent(captured.scope, 'canUseAgent')) onAction(captured.change, 'undo') }}><Button size="small" loading={loading} disabled={!canWrite || !identified || (busy && !loading)}>撤销</Button></Popconfirm>}
        {change.kind === 'task' && change.status === 'executed' && <Typography.Text type="secondary">此记录未保存可验证的子任务身份，请从任务列表查看。</Typography.Text>}
      </Space>
    </Space>
  </Card>
}
