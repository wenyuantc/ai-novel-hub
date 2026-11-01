import { App } from 'antd'
import { createElement, type ReactElement } from 'react'
import { renderToString } from 'react-dom/server'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AdminProject, AdminUserDetail } from '../../api/types'

const state = vi.hoisted(() => ({ mobile: true, admin: true, loading: false, data: undefined as { user: import('../../api/types').AdminUserDetail | null; failure: { status: number; message: string } | null } | { items: import('../../api/types').AdminUserDetail[]; total: number } | undefined, loaders: [] as (() => Promise<unknown>)[] }))
vi.mock('../../hooks/useIsMobile', () => ({ useIsMobile: () => state.mobile, useModalWidth: (width: number | string) => state.mobile ? 'calc(100vw - 24px)' : width }))
vi.mock('../../hooks/useLoader', () => ({ useLoader: (loader: () => Promise<unknown>) => { state.loaders.push(loader); return { data: state.data, loading: state.loading, error: null, reload: () => {} } } }))
vi.mock('../../stores/auth', () => { const snapshot = () => ({ status: 'authenticated', user: { id: 1, username: '管理员', created_at: '', is_admin: state.admin, is_super_admin: state.admin, effective_permissions: [], authorization_revision: 1, roles: [] } }); return { useAuthStore: Object.assign((select: (value: ReturnType<typeof snapshot>) => unknown) => select(snapshot()), { getState: snapshot, subscribe: () => () => {} }) } })
vi.mock('../../api/admin', () => ({ adminApi: { getUser: vi.fn(), listProjects: vi.fn(async () => ({ items: [], total: 0 })), listUsers: vi.fn(async () => ({ items: [], total: 0 })) } }))
vi.mock('./UserQuotaPanel', () => ({ default: () => null }))
vi.mock('../../api/recycleBin', () => ({ recycleBinApi: { list: vi.fn(async () => ({ items: [], total: 0 })) } }))

const { default: AdminUserDetailsPage, AdminUserSummary, AdminUserProjectResults } = await import('../../pages/AdminUserDetailsPage')
const { default: AdminUsersPage } = await import('../../pages/AdminUsersPage')
const { default: AdminUserActionButtons } = await import('./AdminUserActionButtons')
const { default: AdminProjectsPage } = await import('../../pages/AdminProjectsPage')
const { default: AdminRecycleBinPage } = await import('../../pages/AdminRecycleBinPage')
const { adminApi } = await import('../../api/admin')
const { recycleBinApi } = await import('../../api/recycleBin')

const user = (overrides: Partial<AdminUserDetail> = {}): AdminUserDetail => ({ id: 2, username: 'alice', is_admin: false, is_super_admin: false, is_management_account: false, roles: [], authorization_revision: 1, is_active: true, created_at: '2026-10-30T00:00:00Z', project_count: 4, last_login_at: null, usage: { active: { project_count: 2, chapter_count: 3, total_words: 3200 }, recycled: { project_count: 1, chapter_count: 2, total_words: 4500 }, deleting: { project_count: 1, chapter_count: 1, total_words: 600 } }, ...overrides })
const project: AdminProject = { id: 9, title: '星河', genre: '科幻', status: 'writing', owner_user_id: 2, owner_username: 'alice', chapter_count: 2, total_words: 3200, created_at: '2026-10-30T00:00:00Z', updated_at: '2026-11-01T00:00:00Z' }
const render = (element: ReactElement, path = '/') => renderToString(createElement(App, null, createElement(MemoryRouter, { initialEntries: [path] }, element))).replaceAll('<!-- -->', '')
const page = (path = '/admin/users/2') => render(createElement(Routes, null, createElement(Route, { path: '/admin/users/:userId', element: createElement(AdminUserDetailsPage) })), path)

afterEach(() => { state.mobile = true; state.admin = true; state.loading = false; state.data = undefined; state.loaders = []; vi.clearAllMocks() })

describe('账号信息与资源分组', () => {
  it('375px完整展示账号/正常/回收站/删除中，不把删除中伪装成回收站', () => {
    const html = render(createElement(AdminUserSummary, { user: user(), actorId: 1, mobile: true }))
    for (const text of ['用户 ID', 'alice', '普通用户', '注册时间', '最近登录', '暂无记录', '基础资源统计', '正常作品', '回收站', '3,200', '4,500', '1 部作品正在删除处理', '1 章 / 600 字', '暂不计入正常作品或回收站', '到期待清理']) expect(html).toContain(text)
    expect(html).toContain('href="/admin/projects?owner_user_id=2"')
    expect(html).toContain('href="/admin/recycle-bin?owner_user_id=2"')
    expect(html).toContain('grid-template-columns:minmax(0, 1fr)')
    expect(html).not.toMatch(/(?:^|[;"\s])(?:min-)?width:\s*\d{3,}px/)
    expect(html).not.toContain('password_hash')
  })

  it('零资源、自己标识和非空登录时间正常展示，不出现空白计数或删除中提示', () => {
    const zero = { project_count: 0, chapter_count: 0, total_words: 0 }
    const html = render(createElement(AdminUserSummary, { user: user({ id: 1, project_count: 0, last_login_at: '2026-11-01T02:00:00', usage: { active: zero, recycled: zero, deleting: zero } }), actorId: 1, mobile: false }))
    expect(html).toContain('>我<')
    expect(html).not.toContain('暂无记录')
    expect(html).not.toContain('正在删除处理')
    expect(html).toContain('2026-11-01')
    expect(html).toContain('repeat(2, minmax(0, 1fr))')
  })

  it('正常作品桌面与移动展示管理链接，不新增AI/阅读/私人配置入口', () => {
    for (const mobile of [true, false]) {
      const html = render(createElement(AdminUserProjectResults, { items: [project], mobile, loading: false }))
      expect(html).toContain('星河')
      expect(html).toContain('3,200')
      expect(html).toContain('href="/projects/9/overview"')
      expect(html.includes('<table')).toBe(!mobile)
      for (const path of ['/read', '/settings', '/tasks', '/ai']) expect(html).not.toContain(`href="${path}`)
    }
    expect(render(createElement(AdminUserProjectResults, { items: [], mobile: true, loading: false }))).toContain('该用户暂无正常作品')
  })

  it('共享按钮保留本人全部禁用及含回收站作品的删号保护', () => {
    const self = render(createElement(AdminUserActionButtons, { user: user({ id: 1, project_count: 0 }), actorId: 1, disabled: false, onAction: () => {} }))
    expect([...self.matchAll(/<button\b[^>]*disabled=""/g)]).toHaveLength(3)
    const owner = render(createElement(AdminUserActionButtons, { user: user(), actorId: 1, disabled: false, onAction: () => {} }))
    expect([...owner.matchAll(/<button\b[^>]*disabled=""/g)]).toHaveLength(1)
    for (const text of ['重置密码', '禁用', '删除']) expect(owner).toContain(text)
  })
})

describe('详情入口与读取状态', () => {
  it('管理员详情有返回/刷新，普通用户与非法ID不挂载数据读取', () => {
    state.data = { user: user(), failure: null }
    const html = page()
    for (const text of ['返回用户列表', '刷新详情', 'alice 的用户详情', '所属正常作品']) expect(html).toContain(text)
    state.loaders = []
    expect(page('/admin/users/1e2')).toContain('无效的用户 ID')
    expect(state.loaders).toHaveLength(0)
    state.admin = false
    expect(page()).toContain('需要管理员权限')
    expect(state.loaders).toHaveLength(0)
  })

  it('首次加载、404和失败重试都不显示旧用户及危险动作', () => {
    state.loading = true
    expect(page()).toContain('ant-skeleton')
    state.loading = false
    state.data = { user: null, failure: { status: 404, message: '用户不存在' } }
    const missing = page()
    expect(missing).toContain('用户不存在')
    expect(missing).not.toContain('重置密码')
    state.data = { user: null, failure: { status: 0, message: '网络错误，无法连接服务器' } }
    const failed = page()
    expect(failed).toContain('网络错误，无法连接服务器')
    expect(failed).toContain('重试详情')
    expect(failed).not.toContain('alice')
  })

  it('列表用户名及详情入口指向明确的目标账号', () => {
    state.data = { items: [user()], total: 1 }
    const html = render(createElement(AdminUsersPage))
    expect([...html.matchAll(/href="\/admin\/users\/2"/g)]).toHaveLength(2)
    expect(html).toContain('详情')
    expect(html).toContain('alice')
  })

  it('详情正常作品查询固定目标owner；目录与回收站深链每次读取当前owner', async () => {
    state.data = { user: user(), failure: null }
    vi.mocked(adminApi.getUser).mockResolvedValue(user())
    page('/admin/users/2')
    const detailLoaders = [...state.loaders]
    for (const loader of detailLoaders) await loader()
    expect(adminApi.getUser).toHaveBeenCalledWith(2)
    expect(adminApi.listProjects).toHaveBeenCalledWith({ owner_user_id: 2, page: 1, page_size: 20 })
    for (const owner of [7, 9]) {
      state.loaders = []
      render(createElement(AdminProjectsPage), `/admin/projects?owner_user_id=${owner}`)
      for (const loader of state.loaders) await loader()
      expect(adminApi.listProjects).toHaveBeenLastCalledWith(expect.objectContaining({ owner_user_id: owner }))
      state.loaders = []
      render(createElement(AdminRecycleBinPage), `/admin/recycle-bin?owner_user_id=${owner}`)
      for (const loader of state.loaders) await loader()
      expect(recycleBinApi.list).toHaveBeenLastCalledWith(expect.objectContaining({ owner_user_id: owner }))
    }
  })
})
