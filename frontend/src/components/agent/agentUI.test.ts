import { App } from 'antd'
import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { changeFixture } from '../../utils/agent.test'
import AgentChangeCard from './AgentChangeCard'
import AgentMarkdown from './AgentMarkdown'
import AgentDock from './AgentDock'

vi.mock('./AgentPanel', () => ({ default: () => createElement('div', null, '对话') }))
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
