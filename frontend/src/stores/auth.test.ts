import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAuthStore } from './auth'
import { deferred } from '../components/systemSettings/systemSettingsTestFixtures'
import type { User } from '../api/types'

const actor = (overrides: Partial<User> = {}): User => ({ id: 1, username: '作者', is_admin: false, is_super_admin: false, effective_permissions: ['users.read'], authorization_revision: 1, roles: [], created_at: '', ...overrides })
const response = (user: User) => new Response(JSON.stringify(user))

beforeEach(() => {
  useAuthStore.getState().cancelConfig()
  useAuthStore.getState().cancelAuthorization()
  useAuthStore.setState({ status: 'unknown', user: null, registrationEnabled: true, mcpEnabled: true })
})
afterEach(() => { useAuthStore.getState().cancelConfig(); useAuthStore.getState().cancelAuthorization(); vi.unstubAllGlobals() })

describe('私有授权同步的会话与异步隔离', () => {
  it('同时进入管理路由/StrictMode/focus只发一个me；网络失败不登出', async () => {
    useAuthStore.setState({ status: 'authenticated', user: actor() })
    const slow = deferred<Response>()
    const fetcher = vi.fn(() => slow.promise)
    vi.stubGlobal('fetch', fetcher)
    const first = useAuthStore.getState().refreshAuthorization()
    const second = useAuthStore.getState().refreshAuthorization()
    expect(first).toBe(second)
    expect(fetcher).toHaveBeenCalledOnce()
    slow.reject(new TypeError('offline'))
    await first
    expect(useAuthStore.getState()).toMatchObject({ status: 'authenticated', user: actor() })
  })
  it.each([403, 500, 401])('%s仅401清理会话，不修改公开配置', async (status) => {
    useAuthStore.setState({ status: 'authenticated', user: actor(), mcpEnabled: false })
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{"detail":"失败"}', { status })))
    await useAuthStore.getState().refreshAuthorization()
    expect(useAuthStore.getState().status).toBe(status === 401 ? 'anonymous' : 'authenticated')
    expect(useAuthStore.getState().mcpEnabled).toBe(false)
  })
  it('同actor同步撤权，revision不倒退，公开配置刷新不请求me', async () => {
    useAuthStore.setState({ status: 'authenticated', user: actor() })
    const fetcher = vi.fn().mockResolvedValueOnce(response(actor({ authorization_revision: 2, effective_permissions: [] }))).mockResolvedValueOnce(response(actor())).mockResolvedValueOnce(new Response('{"registration_enabled":false}'))
    vi.stubGlobal('fetch', fetcher)
    await useAuthStore.getState().refreshAuthorization()
    expect(useAuthStore.getState().user?.effective_permissions).toEqual([])
    await useAuthStore.getState().refreshAuthorization()
    expect(useAuthStore.getState().user?.authorization_revision).toBe(2)
    await useAuthStore.getState().refreshConfig()
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual(['/api/v1/auth/me', '/api/v1/auth/me', '/api/v1/auth/config'])
  })
  it('忽略中止信号的旧me在切账号后迟到也不覆盖新会话', async () => {
    useAuthStore.setState({ status: 'authenticated', user: actor() })
    const slow = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(slow.promise).mockResolvedValueOnce(response(actor({ id: 2, username: '新账号' }))))
    const old = useAuthStore.getState().refreshAuthorization()
    await useAuthStore.getState().login({ username: '新账号', password: 'password' })
    slow.resolve(response(actor({ is_super_admin: true })))
    await old
    expect(useAuthStore.getState().user?.id).toBe(2)
    expect(useAuthStore.getState().user?.is_super_admin).toBe(false)
  })
  it.each(['login', 'register'] as const)('旧logout在新%s成功后迟到，不能清掉新会话', async (method) => {
    useAuthStore.setState({ status: 'authenticated', user: actor() })
    const slow = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(slow.promise).mockResolvedValueOnce(response(actor({ id: 2 }))))
    const old = useAuthStore.getState().logout()
    await useAuthStore.getState()[method]({ username: 'new', password: 'password' })
    slow.resolve(new Response(null, { status: 204 }))
    await old
    expect(useAuthStore.getState()).toMatchObject({ status: 'authenticated', user: { id: 2 } })
  })
})

describe('认证与公开配置独立同步', () => {
  it('轮询只读公开配置，不重复me、不改变登录态；失败不重开入口', async () => {
    const user = { id: 1, username: '作者', is_admin: false, is_super_admin: false, effective_permissions: [], authorization_revision: 1, roles: [], created_at: '' }
    let offline = false
    const calls: string[] = []
    vi.stubGlobal('fetch', async (url: string) => {
      calls.push(url)
      if (url.endsWith('/auth/me')) return new Response(JSON.stringify(user))
      if (offline) throw new TypeError('offline')
      return new Response(JSON.stringify({ registration_enabled: false, mcp_enabled: false }))
    })
    await useAuthStore.getState().init()
    await useAuthStore.getState().refreshConfig()
    offline = true
    await useAuthStore.getState().refreshConfig()
    expect(calls.filter((url) => url.endsWith('/auth/me'))).toHaveLength(1)
    expect(calls.filter((url) => url.endsWith('/auth/config'))).toHaveLength(3)
    expect(useAuthStore.getState()).toMatchObject({ status: 'authenticated', user, registrationEnabled: false, mcpEnabled: false })
  })
  it('管理员保存应用公开新值后，迟到的init配置不能倒退', async () => {
    const old = deferred<Response>()
    vi.stubGlobal('fetch', async (url: string) => url.endsWith('/auth/me') ? new Response('{}', { status: 401 }) : old.promise)
    const init = useAuthStore.getState().init()
    useAuthStore.getState().applyConfig({ registration_enabled: false, mcp_enabled: false })
    old.resolve(new Response(JSON.stringify({ registration_enabled: true, mcp_enabled: true })))
    await init
    expect(useAuthStore.getState()).toMatchObject({ status: 'anonymous', registrationEnabled: false, mcpEnabled: false })
  })
  it('旧后端缺少MCP字段仍兼容；公开配置不携带管理数据', async () => {
    useAuthStore.getState().applyConfig({ registration_enabled: false })
    expect(useAuthStore.getState()).toMatchObject({ registrationEnabled: false, mcpEnabled: true, status: 'unknown' })
    expect(useAuthStore.getState()).not.toHaveProperty('revision')
    expect(useAuthStore.getState()).not.toHaveProperty('items')
  })
})
