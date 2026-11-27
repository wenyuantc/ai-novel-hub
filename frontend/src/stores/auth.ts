import { create } from 'zustand'
import { accountSecurityApi } from '../api/accountSecurity'
import { authApi } from '../api/auth'
import { ApiError, invalidateRequestGeneration } from '../api/client'
import type { AuthConfig, Credentials, User } from '../api/types'
import { PublicConfigController } from '../utils/publicConfig'

export type AuthStatus = 'unknown' | 'authenticated' | 'anonymous'

interface AuthState {
  status: AuthStatus
  user: User | null
  registrationEnabled: boolean
  /** 当前有效 MCP 开关；刷新失败保留最近成功值，后端始终校验。 */
  mcpEnabled: boolean
  enabledPlugins: string[]
  pluginConfigReady: boolean
  refreshConfig: () => Promise<void>
  applyConfig: (config: AuthConfig) => void
  cancelConfig: () => void
  refreshAuthorization: () => Promise<void>
  cancelAuthorization: () => void
  applyAuthorization: (user: User) => void
  init: () => Promise<void>
  login: (cred: Credentials) => Promise<'challenge' | 'complete' | undefined>
  verify: (code: string) => Promise<void>
  register: (cred: Credentials) => Promise<void>
  logout: (options?: { requireServerConfirmation?: boolean }) => Promise<void>
  /** 会话失效（任意接口 401）时调用 */
  clear: () => void
}

export const useAuthStore = create<AuthState>((set, get) => {
  const config = new PublicConfigController(authApi.config, (value) => set({
    registrationEnabled: value.registration_enabled,
    mcpEnabled: value.mcp_enabled !== false,
    enabledPlugins: value.enabled_plugins ?? [],
    pluginConfigReady: true,
  }))
  let epoch = 0
  let current: { controller: AbortController; promise: Promise<void> } | null = null
  const cancelAuthorization = () => { epoch += 1; invalidateRequestGeneration(); current?.controller.abort(); current = null }
  const clear = () => {
    cancelAuthorization()
    config.cancel()
    set({ status: 'anonymous', user: null, enabledPlugins: [], pluginConfigReady: false })
  }
  const applyAuthorization = (user: User) => {
    if (get().status !== 'authenticated' || get().user?.id !== user.id) return
    cancelAuthorization()
    set({ user })
  }
  return {
  status: 'unknown',
  user: null,
  registrationEnabled: true,
  mcpEnabled: true,
  enabledPlugins: [],
  pluginConfigReady: false,
  refreshConfig: config.refresh,
  applyConfig: config.apply,
  cancelConfig: config.cancel,
  cancelAuthorization,
  applyAuthorization,
  refreshAuthorization: () => {
    const actorId = get().user?.id
    if (get().status !== 'authenticated' || actorId === undefined) return Promise.resolve()
    if (current) return current.promise
    const controller = new AbortController()
    const generation = epoch
    const pending = { controller, promise: Promise.resolve() }
    current = pending
    pending.promise = (async () => {
      try {
        const user = await authApi.me(controller.signal)
        const auth = get()
        if (generation === epoch && !controller.signal.aborted && auth.status === 'authenticated' && auth.user?.id === actorId && user.id === actorId && (user.authorization_revision ?? 0) >= (auth.user.authorization_revision ?? 0)) set({ user })
      } catch (error) {
        if (generation === epoch && !controller.signal.aborted && error instanceof ApiError && error.status === 401) clear()
        // 临时失败保留当前登录与权限快照，后端仍是授权事实源。
      } finally {
        if (current?.controller === controller) current = null
      }
    })()
    return pending.promise
  },

  init: async () => {
    cancelAuthorization()
    const generation = epoch
    const [meResult] = await Promise.allSettled([authApi.me(), config.refresh()])
    if (generation !== epoch) return
    if (meResult.status === 'fulfilled') {
      set({ status: 'authenticated', user: meResult.value })
    } else {
      const err = meResult.reason
      if (!(err instanceof ApiError) || (err.status !== 401 && err.status !== 0)) {
        console.error('获取登录状态失败', err)
      }
      set({ status: 'anonymous', user: null })
    }
  },

  login: async (cred) => {
    cancelAuthorization()
    const generation = epoch
    const user = await authApi.login(cred)
    if (generation !== epoch) return
    if ('two_step_required' in user) {
      set({ status: 'anonymous', user: null })
      return 'challenge'
    }
    set({ status: 'authenticated', user })
    return 'complete'
  },

  verify: async (code) => {
    cancelAuthorization()
    const generation = epoch
    const user = await accountSecurityApi.verify(code)
    if (generation !== epoch) return
    set({ status: 'authenticated', user })
  },

  register: async (cred) => {
    cancelAuthorization()
    const generation = epoch
    const user = await authApi.register(cred)
    if (generation !== epoch) return
    set({ status: 'authenticated', user })
  },

  logout: async (options) => {
    cancelAuthorization()
    const generation = epoch
    try {
      await authApi.logout()
    } catch (err) {
      if (!options?.requireServerConfirmation && generation === epoch) clear()
      throw err
    }
    if (generation === epoch) clear()
  },

  clear,
  }
})
