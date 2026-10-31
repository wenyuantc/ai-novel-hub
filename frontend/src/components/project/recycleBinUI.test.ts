import { App } from 'antd'
import { createElement, type ReactElement } from 'react'
import { renderToString } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RecycleProject } from '../../api/types'

const state = vi.hoisted(() => ({ mobile: true, admin: true, loading: false, error: null as string | null, items: [] as import('../../api/types').RecycleProject[] }))
vi.mock('../../hooks/useIsMobile', () => ({ useIsMobile: () => state.mobile, useModalWidth: (width: number | string) => state.mobile ? 'calc(100vw - 24px)' : width }))
vi.mock('../../hooks/useLoader', () => ({ useLoader: () => ({ data: { items: state.items, total: state.items.length, page: 1 }, loading: state.loading, error: state.error, reload: () => {} }) }))
vi.mock('../../stores/auth', () => ({ useAuthStore: (select: (value: unknown) => unknown) => select({ status: 'authenticated', user: { id: 1, username: '管理员', is_admin: state.admin }, mcpEnabled: false, logout: async () => {} }) }))
vi.mock('../../stores/project', () => ({ useProjectStore: (select: (value: unknown) => unknown) => select({ project: null }) }))
vi.mock('../../stores/tasks', () => ({ useTasksStore: (select: (value: unknown) => unknown) => select({ active: [], loaded: true }) }))
vi.mock('../TaskPanel', () => ({ default: () => null }))
vi.mock('../ChangePasswordModal', () => ({ default: () => null }))
vi.mock('../../theme/ThemeSwitch', () => ({ default: () => null }))

const { default: AdminRecycleBinPage, RecycleProjectResults } = await import('../../pages/AdminRecycleBinPage')
const { RecycleProjectConfirmation } = await import('./RecycleProjectModal')
const { DeleteProjectNotice } = await import('./DeleteProjectModal')
const { default: AppLayout } = await import('../AppLayout')
const noop = () => {}
const render = (element: ReactElement) => renderToString(createElement(App, null, createElement(MemoryRouter, null, element))).replaceAll('<!-- -->', '')
const project = (overrides: Partial<RecycleProject> = {}): RecycleProject => ({
  id: 9, title: '星河', genre: '科幻', status: 'writing', owner_user_id: 2, owner_username: 'alice', deleted_by_user_id: 1, deleted_by_username: '管理员', deleted_at: '2026-10-31T01:00:00.123456Z', expires_at: '2026-11-30T01:00:00.123456Z', restorable: true, chapter_count: 2, total_words: 3200, ...overrides,
})
const now = Date.parse('2026-11-01T00:00:00Z')
afterEach(() => { state.mobile = true; state.admin = true; state.loading = false; state.error = null; state.items = [] })

describe('回收站列表与窄屏布局', () => {
  it('375px 使用可断行卡片而非宽表格，保留归属、删除人、到期与动作', () => {
    const html = render(createElement(RecycleProjectResults, { items: [project({ title: '长标题'.repeat(60) })], mobile: true, loading: false, disabled: false, now, onAction: noop }))
    for (const text of ['alice', '管理员', '可恢复', '删除：', '到期：', '2 章', '3,200 字', '彻底删除', 'overflow-wrap:anywhere']) expect(html).toContain(text)
    expect(html).not.toContain('<table')
    expect(html).not.toMatch(/(?:^|[;"\s])(?:min-)?width:\s*\d{3,}px/)
  })
  it('桌面表格包含必要元数据，不提供正常工作区编辑链接', () => {
    const html = render(createElement(RecycleProjectResults, { items: [project()], mobile: false, loading: false, disabled: false, now, onAction: noop }))
    for (const text of ['作品名称', '所属用户', '删除人', '章节 / 字数', '删除时间', '到期时间', '彻底删除']) expect(html).toContain(text)
    expect(html).toContain('<table')
    expect(html).not.toContain('/projects/9')
  })
  it('已过期不能恢复；空列表与加载状态可识别', () => {
    const expired = render(createElement(RecycleProjectResults, { items: [project({ restorable: false })], mobile: true, loading: false, disabled: false, now, onAction: noop }))
    expect(expired).toContain('已到期，待清理')
    expect(expired).toMatch(/<button[^>]*disabled=""/)
    expect(render(createElement(RecycleProjectResults, { items: [], mobile: true, loading: false, disabled: false, now, onAction: noop }))).toContain('回收站中没有匹配的作品')
    expect(render(createElement(RecycleProjectResults, { items: [], mobile: true, loading: true, disabled: false, now, onAction: noop }))).toContain('ant-spin-spinning')
  })
})

describe('删除/恢复确认语义', () => {
  it('原删除入口说明回收站、保留期与失败结果，不承诺立即永久删除', () => {
    const html = render(createElement(DeleteProjectNotice, { owner: 'alice' }))
    for (const text of ['alice', '移入回收站', '30 天', '仅管理员', '自动取消', '失败或超时', '作品不会删除']) expect(html).toContain(text)
    expect(html).not.toContain('将永久删除')
  })
  it('彻底删除需完整名称，明确不可恢复并保留失败说明', () => {
    const html = render(createElement(RecycleProjectConfirmation, { project: project(), restoring: false, allowed: false, busy: false, input: '', error: '回收站记录已变化', onInput: noop, onSubmit: noop }))
    for (const text of ['无法恢复', '不经过新的 30 天保留期', '请输入完整作品名称', '确认彻底删除的作品名称', '星河', '回收站记录已变化']) expect(html).toContain(text)
  })
  it('名称确认不以 UTF-16 长度截断后端允许的 Unicode 标题', () => {
    const title = '🚀'.repeat(200)
    const html = render(createElement(RecycleProjectConfirmation, { project: project({ title }), restoring: false, allowed: true, busy: false, input: title, error: null, onInput: noop, onSubmit: noop }))
    expect(title.length).toBe(400)
    expect(html).toContain(`value="${title}"`)
    expect(html).not.toMatch(/maxlength="200"/i)
  })
  it('恢复不启用账号和自动续跑，超期提示明确且不提供名称清理输入', () => {
    const html = render(createElement(RecycleProjectConfirmation, { project: project(), restoring: true, allowed: false, busy: false, input: '', error: null, onInput: noop, onSubmit: noop }))
    for (const text of ['原所属用户', '不会启用已禁用的账号', '不会自动重试', '作品已到期']) expect(html).toContain(text)
    expect(html).not.toContain('确认彻底删除的作品名称')
  })
})

describe('仅管理员入口', () => {
  it('普通用户不能渲染回收站，桌面导航不提供入口', () => {
    state.admin = false
    expect(render(createElement(AdminRecycleBinPage))).toContain('需要管理员权限')
    state.mobile = false
    expect(render(createElement(AppLayout))).not.toContain('href="/admin/recycle-bin"')
  })
  it('管理员导航有回收站入口，页名/保留规则明确', () => {
    state.mobile = false
    expect(render(createElement(AppLayout))).toContain('href="/admin/recycle-bin"')
    const html = render(createElement(AdminRecycleBinPage))
    for (const text of ['作品回收站', '仅管理员可恢复', '保留 30 天', '恢复不会自动续跑任务', '搜索回收站作品或用户']) expect(html).toContain(text)
  })
})
