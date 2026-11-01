import { App, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Role, User } from '../api/types'

const state = vi.hoisted(() => ({ mobile: true, superAdmin: true, loading: false, items: [] as import('../api/types').Role[], total: 0, loads: 0 }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => state.mobile }))
vi.mock('../hooks/useLoader', () => ({ useLoader: () => {
  state.loads += 1
  return { data: { roles: { items: state.items, total: state.total, authorization_revision: 1 }, definitions: [] }, loading: state.loading, error: null, reload: () => {} }
} }))
vi.mock('../stores/auth', () => ({ useAuthStore: (select: (value: { user: User }) => unknown) => select({ user: { id: 1, username: 'root', is_admin: state.superAdmin, is_super_admin: state.superAdmin, effective_permissions: ['users.read'], roles: [], authorization_revision: 1, created_at: '' } }) }))

const { default: AdminRolesPage } = await import('./AdminRolesPage')
const role = (overrides: Partial<Role>): Role => ({ id: 1, name: '角色', description: '', permissions: [], system_key: null, is_builtin: false, revision: 1, member_count: 0, created_at: '2026-11-01T12:00:00Z', updated_at: '2026-11-01T12:00:00Z', authorization_revision: 1, ...overrides })
const roles = [
  role({ id: 1, name: '超级管理员', system_key: 'super_admin', is_builtin: true, member_count: 1 }),
  role({ id: 2, name: '长角色名称'.repeat(20), description: '维护内容的详细说明'.repeat(50), member_count: 7, permissions: ['projects.read', 'projects.edit'] }),
  role({ id: 3, name: '未分配角色' }),
]
const render = () => renderToString(createElement(ConfigProvider, { locale: zhCN }, createElement(App, null, createElement(AdminRolesPage)))).replaceAll('<!-- -->', '')
const text = (html: string) => html.replace(/<[^>]+>/g, '').replace(/\s/g, '')
const actionStates = (html: string) => [...html.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)]
  .map((match) => ({ name: text(match[2]), disabled: /\bdisabled=""/.test(match[1]) }))
  .filter((button) => ['复制', '编辑', '删除'].includes(button.name))
const row = (html: string, id: number, mobile: boolean) => {
  const element = mobile ? 'li' : 'tr'
  const key = mobile ? 'data-role-id' : 'data-row-key'
  const match = html.match(new RegExp(`<${element}\\b[^>]*${key}="${id}"[^>]*>[\\s\\S]*?<\\/${element}>`))
  expect(match, `角色 #${id} 的${mobile ? '卡片' : '桌面行'}`).not.toBeNull()
  return match![0]
}

afterEach(() => { state.mobile = true; state.superAdmin = true; state.loading = false; state.items = []; state.total = 0; state.loads = 0 })

describe('角色目录的375px移动卡片与桌面表格', () => {
  it('移动端真实渲染完整角色信息、可换行动作、搜索新建和紧凑分页，不使用横向表格', () => {
    state.items = roles; state.total = 41
    const html = render()
    expect(html).toContain('aria-label="角色移动列表"')
    expect(html).not.toContain('<table')
    for (const value of ['内置只读', '自定义', roles[1].name, roles[1].description, '权限数：全部', '权限数：2', '成员：7', '更新于：', '暂无说明', '共41个角色']) expect(text(html)).toContain(value)
    expect(html).toContain('aria-label="搜索角色"')
    expect(text(html)).toContain('新建角色')
    expect(html).toContain('aria-label="角色分页"')
    expect(actionStates(html)).toHaveLength(9)
    expect(html).toContain('overflow-wrap:anywhere')
    expect(html).toContain('flex-wrap:wrap')
    expect(html).not.toMatch(/(?:^|[;"\s])(?:min-)?width:\s*\d{3,}px/)
    expect(html).not.toContain('100vw')
  })

  it.each([true, false])('%s模式复用相同内置/在用禁用规则：复制始终可用，自定义编辑不受成员数限制', (mobile) => {
    state.mobile = mobile; state.items = roles; state.total = roles.length
    const html = render()
    expect(actionStates(row(html, 1, mobile))).toEqual([{ name: '复制', disabled: false }, { name: '编辑', disabled: true }, { name: '删除', disabled: true }])
    expect(actionStates(row(html, 2, mobile))).toEqual([{ name: '复制', disabled: false }, { name: '编辑', disabled: false }, { name: '删除', disabled: true }])
    expect(actionStates(row(html, 3, mobile))).toEqual([{ name: '复制', disabled: false }, { name: '编辑', disabled: false }, { name: '删除', disabled: false }])
    if (!mobile) {
      expect(html).toContain('<table')
      expect(html).not.toContain('aria-label="角色移动列表"')
      for (const label of ['权限数', '成员', '更新于', '操作']) expect(text(html)).toContain(label)
    }
  })

  it('移动端空结果仍保留搜索、新建与分页入口；非超管不读取目录', () => {
    const empty = render()
    expect(text(empty)).toContain('没有匹配的角色')
    expect(text(empty)).toContain('新建角色')
    expect(empty).toContain('aria-label="搜索角色"')
    expect(empty).toContain('aria-label="角色分页"')
    state.superAdmin = false; state.loads = 0
    const denied = render()
    expect(text(denied)).toContain('只有超级管理员可管理角色')
    expect(denied).not.toContain('新建角色')
    expect(state.loads).toBe(0)
  })
})
