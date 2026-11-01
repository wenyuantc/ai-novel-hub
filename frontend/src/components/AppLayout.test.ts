import { App, type DropdownProps, type MenuProps } from 'antd'
import { createElement, type ReactNode } from 'react'
import { renderToString } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { authApi } from '../api/auth'
import type { User } from '../api/types'
import { useAuthStore } from '../stores/auth'
import { deferred } from './systemSettings/systemSettingsTestFixtures'
import AppLayout from './AppLayout'

const screen = vi.hoisted(() => ({ menus: [] as MenuProps[], navigate: vi.fn() }))
vi.mock('antd', async (original) => {
  const actual = await original<typeof import('antd')>()
  return { ...actual, Dropdown: (props: DropdownProps) => {
    if (props.menu) screen.menus.push(props.menu)
    return createElement(actual.Dropdown, props)
  } }
})
vi.mock('../stores/auth', async (original) => {
  const actual = await original<typeof import('../stores/auth')>()
  const hook = (selector: (state: ReturnType<typeof actual.useAuthStore.getState>) => unknown) => selector(actual.useAuthStore.getState())
  return { ...actual, useAuthStore: Object.assign(hook, actual.useAuthStore) }
})
vi.mock('react-router-dom', () => ({
  Link: ({children, to}: {children: ReactNode; to: string}) => createElement('a', {href: to}, children),
  Outlet: () => null,
  useLocation: () => ({pathname: '/admin/tasks', key: 'new-account-page'}),
  useNavigate: () => screen.navigate,
}))
vi.mock('./TaskPanel', () => ({default: () => null}))
vi.mock('./ChangePasswordModal', () => ({default: () => null}))
vi.mock('../theme/ThemeSwitch', () => ({default: () => null}))
vi.mock('../hooks/useIsMobile', () => ({useIsMobile: () => false}))

const user = (id: number): User => ({ id, username: `账号${id}`, is_admin: false, is_super_admin: false, effective_permissions: ['tasks.read'], authorization_revision: 1, roles: [], created_at: '' })
afterEach(() => { useAuthStore.getState().clear(); screen.menus = []; screen.navigate.mockClear(); vi.restoreAllMocks() })

describe('外壳账号菜单的迟到退出回调', () => {
  it.each([false, true])('旧logout等待期间新登录=%s：只为仍匿名会话导航', async (newLogin) => {
    const slow = deferred<void>()
    vi.spyOn(authApi, 'logout').mockReturnValue(slow.promise)
    vi.spyOn(authApi, 'login').mockResolvedValue(user(2))
    useAuthStore.setState({status: 'authenticated', user: user(1)})
    renderToString(createElement(App, null, createElement(AppLayout)))
    const menu = screen.menus.find((item) => item.items?.some((entry) => entry && 'key' in entry && entry.key === 'logout'))
    expect(menu).toBeDefined()
    const pending = Reflect.apply(menu!.onClick!, undefined, [{key: 'logout'}])
    if (newLogin) await useAuthStore.getState().login({username: '账号2', password: 'secret123'})
    slow.resolve()
    await pending
    if (newLogin) {
      expect(useAuthStore.getState().user?.id).toBe(2)
      expect(screen.navigate).not.toHaveBeenCalled()
    } else {
      expect(useAuthStore.getState().status).toBe('anonymous')
      expect(screen.navigate).toHaveBeenCalledExactlyOnceWith('/login', {replace: true})
    }
  })
})
