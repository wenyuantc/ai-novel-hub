import { create } from 'zustand'
import { authApi } from '../api/auth'
import { ApiError } from '../api/client'
import type { Credentials, User } from '../api/types'

export type AuthStatus = 'unknown' | 'authenticated' | 'anonymous'

interface AuthState {
  status: AuthStatus
  user: User | null
  registrationEnabled: boolean
  /** 部署是否开启 MCP 插件（/auth/config；读取失败时按开启处理，插件页自己会显示 403） */
  mcpEnabled: boolean
  init: () => Promise<void>
  login: (cred: Credentials) => Promise<void>
  register: (cred: Credentials) => Promise<void>
  logout: () => Promise<void>
  /** 会话失效（任意接口 401）时调用 */
  clear: () => void
}

export const useAuthStore = create<AuthState>((set) => ({
  status: 'unknown',
  user: null,
  registrationEnabled: true,
  mcpEnabled: true,

  init: async () => {
    const [meResult, cfgResult] = await Promise.allSettled([authApi.me(), authApi.config()])
    const registrationEnabled = cfgResult.status === 'fulfilled' ? cfgResult.value.registration_enabled : true
    const mcpEnabled = cfgResult.status === 'fulfilled' ? cfgResult.value.mcp_enabled !== false : true
    if (meResult.status === 'fulfilled') {
      set({ status: 'authenticated', user: meResult.value, registrationEnabled, mcpEnabled })
    } else {
      const err = meResult.reason
      if (!(err instanceof ApiError) || (err.status !== 401 && err.status !== 0)) {
        console.error('获取登录状态失败', err)
      }
      set({ status: 'anonymous', user: null, registrationEnabled, mcpEnabled })
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
}))
