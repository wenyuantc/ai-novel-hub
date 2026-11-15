import { App } from 'antd'
import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { StaticRouter } from 'react-router-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useAuthStore } from '../../stores/auth'
import { AGENT_RETRY_REJECTED_HINT, AGENT_MODEL_CHANGED_HINT, AGENT_VOIDED_ERROR } from '../../utils/agent'
import { AGENT_OWNER_MODEL_COST, AGENT_OWNER_MODEL_VECTOR_COST, AGENT_OWNER_VECTOR_COST } from '../../utils/kb'
import AgentTimeline from './AgentTimeline'
import { changeFixture } from '../../utils/agent.test'
import AgentChangeCard from './AgentChangeCard'
import AgentMarkdown from './AgentMarkdown'
import AgentDock from './AgentDock'
import AgentConversationView from './AgentConversationView'

const TURN_KEY = '00000000-0000-4000-8000-000000000003'
const view = vi.hoisted(() => ({
  conversation: {} as Record<string, unknown>, source: 'own' as string,
  indexStatus: undefined as unknown, indexOptions: [] as { enabled?: boolean }[],
}))
vi.mock('./AgentPanel', () => ({ default: () => createElement('div', null, '对话') }))
vi.mock('./useAgentConversation', () => ({ useAgentConversation: () => view.conversation }))
vi.mock('../../hooks/useAIModelSource', () => ({ useAIModelSource: () => view.source }))
vi.mock('../../hooks/useIndexStatus', () => ({
  useIndexStatus: (_pid: number, options: { enabled?: boolean }) => {
    view.indexOptions.push(options)
    return { status: options.enabled ? view.indexStatus : undefined }
  },
}))
const conversation = (over: Record<string, unknown> = {}) => ({
  historySealed: false, busy: false, steps: [], stream: { running: false, text: '' }, collaborator: false, writable: true, canCreate: true,
  readOnlyNotice: null, retryAllowed: false, retryHint: null, lastTask: null, error: null, actingKey: null,
  refresh: () => {}, send: async () => true, change: async () => true, turn: async () => true, cancel: async () => true,
  retry: async () => true, update: async () => true,
  detail: { auto_execute: false, mcp_plugin_ids: [], active_task: { id: 8, task_key: TURN_KEY, status: 'paused' },
    messages: [{ id: 1, conversation_id: 2, turn_task_id: 8, turn_task_key: TURN_KEY, role: 'user', content: '仍可阅读的原消息' }],
    changes: [changeFixture({ turn_task_id: 8, status: 'proposed' })] },
  ...over,
})
const renderView = (over: Record<string, unknown> = {}) => {
  view.conversation = conversation(over)
  const element = createElement(AgentConversationView, { pid: 3, cid: 2, onUpdated: () => {}, onCreate: () => {}, creating: false })
  return renderToString(createElement(App, null, createElement(StaticRouter, { location: '/projects/3/overview' }, element))).replaceAll('<!-- -->', '')
}
beforeEach(() => { view.source = 'own'; view.indexStatus = undefined; view.indexOptions = []; useAuthStore.setState({ mcpEnabled: true }) })
// Drawer 使用 portal；node 测试检查传给实际容器的移动端宽度和开关语义。
vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  return { ...actual, Drawer: ({ width, open, children }: { width: string; open: boolean; children: React.ReactNode }) => createElement('section', { 'data-mobile-drawer': open, style: { width } }, children) }
})
const renderCard = (status: ReturnType<typeof changeFixture>['status']) => renderToString(createElement(App, null, createElement(AgentChangeCard, { change: changeFixture({ status }), onAction: () => {} })))
const externalChange = (over: Partial<ReturnType<typeof changeFixture>> = {}) => changeFixture({
  kind: 'external', tool_name: 'mcp__dict__lookup', arguments: { q: '剑' },
  preview: { title: '调用外部插件「词典」的工具 lookup', target: '词典', diff: [], warnings: ['外部操作，执行后无法撤销'], external: { plugin: { id: 5, slug: 'dict', name: '词典' }, tool: 'lookup', arguments: { q: '剑', limit: 3 }, read_only_hint: true } },
  ...over,
})
const renderExternal = (over: Partial<ReturnType<typeof changeFixture>> = {}, props: { acting?: boolean; busy?: boolean } = {}) =>
  renderToString(createElement(App, null, createElement(AgentChangeCard, { change: externalChange(over), onAction: () => {}, ...props }))).replaceAll('<!-- -->', '')

describe('智能体 UI', () => {
  it('封存后显示历史和新开入口，不提供消息发送，旧提案不可继续', () => {
    const html = renderView({ historySealed: true })
    expect(html).toContain('仍可阅读的原消息')
    expect(html).toContain('此会话仅供查看')
    expect(html).toContain('新开会话')
    expect(html).toContain('旧会话不能继续，可以停止这一轮。')
    expect(html).not.toContain('发给项目智能体的消息')
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*><span>确\s*认<\/span><\/button>/)
  })
  it('查看者的封存会话：仍可阅读历史，但不提供新开会话入口', () => {
    const html = renderView({ historySealed: true, writable: false, canCreate: false, collaborator: true })
    expect(html).toContain('仍可阅读的原消息')
    expect(html).toContain('此会话仅供查看')
    expect(html).not.toContain('新开会话')
    expect(html).not.toContain('发给项目智能体的消息')
  })
  it('作者可写会话：输入框、自动执行与 MCP 设置可用', () => {
    const html = renderView()
    expect(html).toContain('发给项目智能体的消息')
    expect(html).toContain('自动执行小修改')
    expect(html).toContain('启用 MCP 插件')
    expect(html).not.toContain('此会话仅供查看')
    expect(html).toMatch(/<button[^>]*><span>确\s*认<\/span><\/button>/)
    expect(html).not.toMatch(/<button[^>]*disabled=""[^>]*><span>确\s*认<\/span><\/button>/)
  })
  it('可写编辑者：不显示 MCP 设置（裁决 2）；回退作者模型时说明外部费用由作者承担', () => {
    view.source = 'owner'
    const html = renderView({ collaborator: true })
    expect(html).toContain('发给项目智能体的消息')
    expect(html).toContain('自动执行小修改')
    expect(html).not.toContain('MCP')
    expect(html).toContain('外部服务费用由作者账户承担')
    view.source = 'own'
    expect(renderView({ collaborator: true })).not.toContain('外部服务费用由作者账户承担')
  })
  it('裁决 31：可写协作者在服务端确认向量检索时提示向量费用；模型来源为作者时合并一行；作者与查看者不读取', () => {
    const vector = { embedding_model: null, needs_rebuild: false, stale_count: 0, stale: { kb: 0, memory: 0 }, indexing: false,
      owner_embedding_configured: true, retrieval_mode: 'vector', keyword_reason: null }
    view.indexStatus = vector
    expect(renderView({ collaborator: true })).toContain(AGENT_OWNER_VECTOR_COST)
    expect(view.indexOptions.at(-1)?.enabled).toBe(true)
    view.source = 'owner'
    const merged = renderView({ collaborator: true })
    expect(merged).toContain(AGENT_OWNER_MODEL_VECTOR_COST)
    expect(merged).not.toContain(AGENT_OWNER_VECTOR_COST)
    expect(merged).not.toContain(AGENT_OWNER_MODEL_COST)
    view.source = 'own'
    view.indexStatus = { ...vector, retrieval_mode: 'keyword', keyword_reason: 'owner_embedding_not_allowed' }
    expect(renderView({ collaborator: true })).not.toContain('费用由作者')
    view.indexStatus = undefined
    expect(renderView({ collaborator: true })).not.toContain('费用由作者')
    view.indexStatus = vector
    view.indexOptions = []
    expect(renderView()).not.toContain('费用由作者')
    expect(view.indexOptions.every((o) => o.enabled === false)).toBe(true)
    expect(renderView({ collaborator: true, writable: false, readOnlyNotice: '只读' })).not.toContain('费用由作者')
    expect(view.indexOptions.every((o) => o.enabled === false)).toBe(true)
    // 封存会话（即使 writable）也不读取 index-status
    expect(renderView({ collaborator: true, historySealed: true })).not.toContain('费用由作者')
    expect(view.indexOptions.every((o) => o.enabled === false)).toBe(true)
    // 可写协作者：只在页面可见时轮询（foreground），读取键随授权轮次 / 模型授权变化
    view.indexOptions = []
    renderView({ collaborator: true })
    expect(view.indexOptions.at(-1)).toMatchObject({ enabled: true, foreground: true })
    expect(typeof (view.indexOptions.at(-1) as { revalidateKey?: unknown }).revalidateKey).toBe('string')
  })
  it.each([
    ['查看者', '当前为查看者，只能查看本人会话，不能发送、确认、拒绝或撤销。'],
    ['重邀后的旧会话', '此会话属于之前的协作授权，只能查看；请新开会话继续。'],
  ])('%s（writable=false）：只读查看，输入 / 确认 / 拒绝 / 撤销不可用', (_name, notice) => {
    const executed = changeFixture({ id: 2, change_key: '00000000-0000-4000-8000-000000000002', turn_task_id: 8, status: 'executed', seq: 2 })
    const html = renderView({ collaborator: true, writable: false, readOnlyNotice: notice,
      detail: { ...conversation().detail, changes: [changeFixture({ turn_task_id: 8, status: 'proposed' }), executed] } })
    expect(html).toContain('此会话仅供查看')
    expect(html).toContain(notice)
    expect(html).toContain('仍可阅读的原消息')
    for (const text of ['发给项目智能体的消息', '自动执行小修改', 'MCP', '停止', '重试本轮']) expect(html).not.toContain(text)
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*><span>确\s*认<\/span><\/button>/)
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*><span>拒\s*绝<\/span><\/button>/)
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*><span>撤\s*销<\/span><\/button>/)
  })
  it('协作者重试：模型配置失效的失败隐藏重试并提示；被拒绝后统一提示', () => {
    const failed = { id: 8, task_key: TURN_KEY, status: 'failed', kind: 'agent.turn', error: '模型配置已失效，请重新选择' }
    const base = { collaborator: true, lastTask: failed, detail: { ...conversation().detail, active_task: null } }
    let html = renderView({ ...base, retryAllowed: false, retryHint: AGENT_MODEL_CHANGED_HINT })
    expect(html).toContain('模型配置已失效，请重新选择')
    expect(html).toContain(AGENT_MODEL_CHANGED_HINT)
    expect(html).not.toContain('重试本轮')
    html = renderView({ ...base, lastTask: { ...failed, error: '模型调用失败' }, retryAllowed: false, retryHint: AGENT_RETRY_REJECTED_HINT })
    expect(html).toContain(AGENT_RETRY_REJECTED_HINT)
    expect(html).not.toContain('重试本轮')
    expect(renderView({ ...base, lastTask: { ...failed, error: '模型调用失败' }, retryAllowed: true })).toContain('重试本轮')
  })
  it('时间线：作废提案显示失败原因；已结束轮次残留的待确认提案置灰为「轮次已结束」', () => {
    const voided = changeFixture({ id: 2, change_key: '00000000-0000-4000-8000-000000000002', status: 'failed', error: AGENT_VOIDED_ERROR, seq: 2 })
    const leftover = changeFixture({ id: 3, change_key: '00000000-0000-4000-8000-000000000004', status: 'proposed', seq: 3 })
    const props = { messages: [], changes: [voided, leftover], steps: [], text: '', running: false, busy: false,
      confirmationReady: false, onAction: () => {}, onTurn: () => {} }
    const ended = renderToString(createElement(App, null, createElement(AgentTimeline, {
      ...props, confirmationTask: { id: 4, task_key: TURN_KEY, status: 'failed' as const } }))).replaceAll('<!-- -->', '')
    expect(ended).toContain('已作废')
    expect(ended).toContain(AGENT_VOIDED_ERROR)
    expect(ended).toContain('轮次已结束')
    expect(ended).toContain('opacity:0.6')
    expect(ended).not.toContain('>确')
    expect(ended).not.toContain('全部确认')
    const open = renderToString(createElement(App, null, createElement(AgentTimeline, {
      ...props, confirmationReady: true, confirmationTask: { id: 4, task_key: TURN_KEY, status: 'paused' as const } }))).replaceAll('<!-- -->', '')
    expect(open).not.toContain('轮次已结束')
    expect(open).toContain('待确认')
    expect(open).toContain('>确')
  })
  it('时间线：已结束轮次（作废 / 失败 / 取消）中无结果的工具步骤显示「已停止」；运行中 / 待确认 / 实时步骤仍「处理中」（E4i-h）', () => {
    const call = { id: 'call_1', name: 'update_chapter_meta', arguments: '{}' }
    const assistant = { id: 1, conversation_id: 2, turn_task_id: 4, turn_task_key: TURN_KEY, role: 'assistant' as const, content: '', tool_calls: [call],
      tool_call_id: null, tool_name: null, page_context: null, usage: null, created_at: '', updated_at: '' }
    const voided = changeFixture({ id: 2, change_key: '00000000-0000-4000-8000-000000000002', tool_name: 'update_chapter_meta', status: 'failed', error: AGENT_VOIDED_ERROR })
    const render = (status: 'failed' | 'cancelled' | 'running' | 'paused' | null, extra: Record<string, unknown> = {}) => renderToString(createElement(App, null, createElement(AgentTimeline, {
      messages: [assistant], changes: [voided], steps: [], text: '', running: false, busy: false, confirmationReady: false, onAction: () => {}, onTurn: () => {},
      confirmationTask: status ? { id: 4, task_key: TURN_KEY, status } : null, ...extra }))).replaceAll('<!-- -->', '')
    for (const status of ['failed', 'cancelled', null] as const) {
      const html = render(status)
      expect(html).toContain('修改章节标题 · 已停止')
      expect(html).not.toContain('处理中')
      expect(html).toContain('已作废')
    }
    for (const status of ['running', 'paused'] as const) expect(render(status)).toContain('修改章节标题 · 处理中')
    // 作者实时订阅中的步骤：会话详情尚未刷新出活动轮次也保持「处理中」
    const live = render(null, { running: true, steps: [{ call_id: 'call_1', name: 'update_chapter_meta', label: '修改章节标题', turn_task_id: 4, turn_task_key: TURN_KEY }] })
    expect(live).toContain('修改章节标题 · 处理中')
  })
  it('提案显示字段前后对比；按钮随 proposed/executed/undone/rejected 状态切换', () => {
    const proposed = renderCard('proposed')
    expect(proposed).toContain('急躁')
    expect(proposed).toContain('冷静')
    expect(proposed).toContain('>确')
    expect(proposed).toContain('>拒')
    expect(renderCard('executed')).toContain('>撤')
    expect(renderCard('undone')).not.toContain('<button')
    expect(renderCard('rejected')).not.toContain('<button')
  })
  it('缺提案版本或原轮次身份时禁用确认与撤销；无法证明的子任务不按整数 ID 查询', () => {
    const renderMissing = (over: Partial<ReturnType<typeof changeFixture>>) => renderToString(createElement(App, null,
      createElement(AgentChangeCard, { change: changeFixture(over), onAction: () => {} }))).replaceAll('<!-- -->', '')
    const noVersion = renderMissing({ view_version: null })
    expect(noVersion).toContain('提案身份或预览版本不可确认')
    expect(noVersion).toMatch(/<button[^>]*disabled=""[^>]*><span>确\s*认<\/span><\/button>/)
    const noTurn = renderMissing({ status: 'executed', turn_task_key: null })
    expect(noTurn).toMatch(/<button[^>]*disabled=""[^>]*><span>撤\s*销<\/span><\/button>/)
    const child = renderMissing({ kind: 'task', status: 'executed', result: { task_id: 9 } })
    expect(child).toContain('此记录未保存可验证的子任务身份')
    expect(child).not.toContain('查看任务')
  })
  it('外部调用提案：显示插件、工具、完整参数与「无法撤销」，可确认 / 拒绝', () => {
    const html = renderExternal()
    expect(html).toContain('外部操作，无法撤销')
    expect(html).toContain('插件：词典（dict）')
    expect(html).toContain('lookup')
    expect(html).toContain('&quot;limit&quot;: 3')
    expect(html).toContain('插件声明只读')
    expect(html).toContain('>确认调用')
    expect(html).toContain('>拒')
    expect(html).not.toContain('<table')
  })
  it('外部调用确认中显示加载状态；只有正在处理的卡片转圈', () => {
    const acting = renderExternal({}, { busy: true, acting: true })
    expect(acting).toContain('ant-btn-loading')
    expect(acting).toContain('最长约 30 秒')
    const other = renderExternal({}, { busy: true, acting: false })
    expect(other).not.toContain('ant-btn-loading')
    expect(other).toContain('disabled=""')
  })
  it('外部调用执行后显示结果，不显示撤销；失败显示原因', () => {
    const executed = renderExternal({ status: 'executed', result: { text: '剑：古代兵器' } })
    expect(executed).toContain('剑：古代兵器')
    expect(executed).not.toContain('>撤')
    expect(executed).not.toContain('<button')
    const failed = renderExternal({ status: 'failed', result: { error: 'MCP 工具调用超时' }, error: 'MCP 工具调用超时' })
    expect(failed).toContain('调用失败')
    expect(failed).not.toContain('<button')
  })
  it('安全渲染 markdown，HTML和不安全链接永远不作为可执行内容', () => {
    const html = renderToString(createElement(AgentMarkdown, { text: '**冷静**\n<script>alert(1)</script>\n[危险](javascript:alert(1))\n[资料](https://example.com)\n```html\n<img onerror=alert(1)>\n```' }))
    expect(html).toContain('<strong>冷静</strong>')
    expect(html).toContain('&lt;script&gt;')
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('href="javascript:')
    expect(html).toContain('href="https://example.com"')
    expect(html).toContain('rel="noopener noreferrer"')
  })
  it('移动端使用全宽抽屉；桌面可键盘调宽，折叠时不挂载面板', () => {
    const props = { projectId: 3, open: true, mobile: true, width: 400, onWidth: () => {}, onClose: () => {} }
    const mobile = renderToString(createElement(AgentDock, props))
    expect(mobile).toContain('data-mobile-drawer="true"')
    expect(mobile).toContain('width:100%')
    expect(mobile).not.toContain('role="separator"')
    const desktop = renderToString(createElement(AgentDock, { ...props, mobile: false }))
    expect(desktop).toContain('role="separator"')
    expect(desktop).toContain('aria-valuemin="320"')
    expect(desktop).toContain('aria-valuemax="720"')
    expect(desktop).toContain('tabindex="0"')
    expect(renderToString(createElement(AgentDock, { ...props, open: false, mobile: false }))).toBe('')
  })
})
