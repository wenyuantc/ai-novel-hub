import { create } from 'zustand'
import { authApi } from '../api/auth'
import { ApiError } from '../api/client'
import type { AuthConfig, Credentials, User } from '../api/types'
import { PublicConfigController } from '../utils/publicConfig'

export type AuthStatus = 'unknown' | 'authenticated' | 'anonymous'

interface AuthState {
  status: AuthStatus
  user: User | null
  registrationEnabled: boolean
  /** 当前有效 MCP 开关；刷新失败保留最近成功值，后端始终校验。 */
  mcpEnabled: boolean
  refreshConfig: () => Promise<void>
  applyConfig: (config: AuthConfig) => void
  cancelConfig: () => void
  init: () => Promise<void>
  login: (cred: Credentials) => Promise<void>
  register: (cred: Credentials) => Promise<void>
  logout: () => Promise<void>
  /** 会话失效（任意接口 401）时调用 */
  clear: () => void
}

export const useAuthStore = create<AuthState>((set) => {
  const config = new PublicConfigController(authApi.config, (value) => set({ registrationEnabled: value.registration_enabled, mcpEnabled: value.mcp_enabled !== false }))
  return {
  status: 'unknown',
  user: null,
  registrationEnabled: true,
  mcpEnabled: true,
  refreshConfig: config.refresh,
  applyConfig: config.apply,
  cancelConfig: config.cancel,

  init: async () => {
    const [meResult] = await Promise.allSettled([authApi.me(), config.refresh()])
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
    const user = await authApi.login(cred)
    set({ status: 'authenticated', user })
  },

  register: async (cred) => {
    const user = await authApi.register(cred)
    set({ status: 'authenticated', user })
  },

  logout: async () => {
    try {
      await authApi.logout()
    } finally {
      set({ status: 'anonymous', user: null })
    }
  },

  clear: () => set({ status: 'anonymous', user: null }),
  }
})
