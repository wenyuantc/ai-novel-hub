import { api, request } from './client'
import type { User } from './types'

export type LoginProvider = 'linuxdo' | 'github'
export const providerName: Record<LoginProvider, string> = { linuxdo: 'Linux DO', github: 'GitHub' }

export const accountSecurityApi = {
  providers: () => api.get<{ providers: LoginProvider[] }>('/auth/oauth/providers'),
  identities: () => api.get<{ providers: LoginProvider[]; has_local_password: boolean }>('/auth/oauth/identities'),
  start: (provider: LoginProvider, intent: 'login' | 'bind') =>
    request<{ url: string }>(`/auth/oauth/${provider}/start`, { method: 'POST', query: { intent } }),
  unbind: (provider: LoginProvider) => api.del(`/auth/oauth/${provider}/binding`),
  status: () => api.get<{ enabled: boolean }>('/auth/two-step/status'),
  setup: () => api.post<{ secret: string; otpauth_uri: string }>('/auth/two-step/setup'),
  enable: (code: string) => api.post<{ recovery_codes: string[] }>('/auth/two-step/enable', { code }),
  disable: (code: string) => api.post<void>('/auth/two-step/disable', { code }),
  verify: (code: string) => api.post<User>('/auth/two-step/verify', { code }),
  reset: (userId: number) => api.post<void>(`/admin/users/${userId}/reset-two-step`, { identity_verified: true }),
}
