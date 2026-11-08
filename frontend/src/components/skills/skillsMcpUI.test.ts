// Skill / MCP 页面组件的渲染（node 环境，react-dom/server）：只读权限、请求头掩码、375 宽度下不写固定宽度
import { App } from 'antd'
import { createElement, type ReactElement } from 'react'
import { renderToString } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { McpPlugin } from '../../api/types'
import { skillFixture } from '../../utils/skills.test'
import { headerRow } from '../../utils/mcp'

const mobile = vi.hoisted(() => ({ value: true }))
vi.mock('../../hooks/useIsMobile', () => ({
  useIsMobile: () => mobile.value,
  useModalWidth: (w: number | string) => (mobile.value ? 'calc(100vw - 24px)' : w),
}))

// zustand 在服务端渲染时读取初始状态（getServerSnapshot），所以直接替换 store 模块
const seeded = vi.hoisted(() => ({ skills: [] as import('../../api/types').SkillBrief[] }))
vi.mock('../../stores/skills', () => ({
  useSkills: () => ({ skills: seeded.skills, loading: false, error: null }),
  useSkillStore: (select: (s: { load: () => Promise<void> }) => unknown) => select({ load: async () => {} }),
}))
vi.mock('../../stores/auth', () => ({ useAuthStore: (select: (s: { user: import('../../api/types').User }) => unknown) => select({ user: { id: 1, username: '作者', created_at: '', is_admin: false, is_super_admin: false, effective_permissions: [], authorization_revision: 1, roles: [] } }) }))

const { default: SkillCard } = await import('./SkillCard')
const { default: SkillsPage } = await import('../../pages/SkillsPage')
const { default: HeadersEditor, MASK } = await import('../mcp/HeadersEditor')
const { default: McpPluginCard } = await import('../mcp/McpPluginCard')

// 去掉 React 在相邻文本节点之间插入的 <!-- --> 便于断言
// SkillsPage 读取 ?skill= 参数，需要 Router 上下文
const render = (el: ReactElement) => renderToString(createElement(MemoryRouter, null, createElement(App, null, el))).replaceAll('<!-- -->', '')
const noop = () => {}
const pluginFixture = (over: Partial<McpPlugin> = {}): McpPlugin => ({
  id: 1, slug: 'dict', name: '词典', transport: 'streamable_http', url: 'https://example.com/a/very/long/path/that/should/wrap/mcp?token-free=1',
  enabled: true, header_names: ['Authorization'], has_headers: true,
  tools: [{ name: 'lookup', description: '查词', input_schema: { type: 'object' }, read_only_hint: true, schema_hash: 'h', enabled: true, auto_confirm: false }],
  tools_refreshed_at: '2026-10-27T08:00:00', last_tested_at: '2026-10-27T08:00:00', last_error: '', created_at: '', updated_at: '', ...over,
})
/** 375 宽度下的页面级横向溢出来源：固定的大宽度 / 最小宽度、文档流里的 100vw */
function expectNoFixedWidth(html: string) {
  expect(html).not.toMatch(/(?:^|[;"\s])(?:min-)?width:\s*\d{3,}px/)
  expect(html).not.toContain('100vw')
}

describe('Skill 卡片权限', () => {
  it('全局 Skill 对普通用户只读：查看 / 复制为我的 / 导出，没有编辑、删除、恢复内置', () => {
    const html = render(createElement(SkillCard, { skill: skillFixture({ scope: 'global', editable: false, builtin_key: 'deslop', name: '去 AI 味' }), authorization: { is_super_admin: false, effective_permissions: [] }, onAction: noop }))
    expect(html).toContain('只读')
    expect(html).toContain('查看')
    expect(html).toContain('复制为我的')
    expect(html).toContain('导出')
    expect(html).not.toContain('编辑')
    expect(html).not.toContain('删除')
    expect(html).not.toContain('恢复内置')
  })
  it('管理员可编辑全局内置 Skill 并恢复内置内容；我的 Skill 可编辑删除', () => {
    const admin = render(createElement(SkillCard, { skill: skillFixture({ scope: 'global', editable: true, builtin_key: 'deslop' }), authorization: { is_super_admin: true, effective_permissions: [] }, onAction: noop }))
    expect(admin).toContain('编辑')
    expect(admin).toContain('删除')
    expect(admin).toContain('恢复内置')
    expect(admin).not.toContain('只读')
    const mine = render(createElement(SkillCard, { skill: skillFixture(), authorization: { is_super_admin: false, effective_permissions: [] }, onAction: noop }))
    expect(mine).toContain('编辑')
    expect(mine).toContain('删除')
    expect(mine).toContain('/续写')
    expect(mine).not.toContain('恢复内置')
  })
})

describe('请求头编辑', () => {
  it('已保存的值只显示名称与掩码，不渲染输入框', () => {
    const html = render(createElement(HeadersEditor, { value: { mode: 'keep' }, onChange: noop, savedNames: ['Authorization', 'X-Key'] }))
    expect(html).toContain(`Authorization: ${MASK}`)
    expect(html).toContain(`X-Key: ${MASK}`)
    expect(html).toContain('重新设置')
    expect(html).toContain('清空请求头')
    expect(html).not.toContain('<input')
  })
  it('重新设置后用密码框填写取值，可取消保持原值', () => {
    const html = render(createElement(HeadersEditor, { value: { mode: 'edit', rows: [headerRow('Authorization')] }, onChange: noop, savedNames: ['Authorization'] }))
    expect(html).toContain('type="password"')
    expect(html).toContain('取消，保持原值')
    expect(html).toContain('整体替换')
  })
})

describe('375 宽度渲染', () => {
  it('Skill 页（我的 / 全局 Tab、卡片）没有固定宽度', () => {
    seeded.skills = [skillFixture(), skillFixture({ id: 2, scope: 'global', editable: false, name: '全局'.repeat(20) })]
    const html = render(createElement(SkillsPage))
    expect(html).toContain('我的（1）')
    expect(html).toContain('全局（1）')
    expect(html).toContain('导入 SKILL.md / zip')
    expectNoFixedWidth(html)
  })
  it('插件卡片：长地址可换行、显示状态与工具数量，不显示请求头取值', () => {
    const html = render(createElement(McpPluginCard, { plugin: pluginFixture(), onAction: noop, onToggle: noop }))
    expect(html).toContain('已连接')
    expect(html).toContain('工具 1 个（启用 1）')
    expect(html).toContain('请求头 1 个')
    expect(html).toContain('break-all')
    expectNoFixedWidth(html)
    const failed = render(createElement(McpPluginCard, { plugin: pluginFixture({ last_error: '连接被拒绝' }), onAction: noop, onToggle: noop }))
    expect(failed).toContain('连接失败')
    expect(failed).toContain('连接被拒绝')
  })
  it('请求头编辑行在窄屏下换行', () => {
    const html = render(createElement(HeadersEditor, { value: { mode: 'edit', rows: [headerRow('A', 'b')] }, onChange: noop, savedNames: [] }))
    expect(html).toContain('flex-wrap:wrap')
    expectNoFixedWidth(html)
  })
})
