import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAuthStore } from './auth'
import { deferred } from '../components/systemSettings/systemSettingsTestFixtures'

beforeEach(() => {
  useAuthStore.getState().cancelConfig()
  useAuthStore.setState({ status: 'unknown', user: null, registrationEnabled: true, mcpEnabled: true })
})
afterEach(() => { useAuthStore.getState().cancelConfig(); vi.unstubAllGlobals() })

describe('认证与公开配置独立同步', () => {
  it('轮询只读公开配置，不重复me、不改变登录态；失败不重开入口', async () => {
    const user = { id: 1, username: '作者', is_admin: false, created_at: '' }
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
