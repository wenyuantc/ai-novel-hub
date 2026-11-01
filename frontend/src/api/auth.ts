import { api, request } from './client'
import type { AuthConfig, ChangePasswordRequest, Credentials, User } from './types'

export const authApi = {
  config: (signal?: AbortSignal) => request<AuthConfig>('/auth/config', { signal }),
  me: (signal?: AbortSignal) => request<User>('/auth/me', { signal }),
  login: (body: Credentials) => api.post<User>('/auth/login', body),
  register: (body: Credentials) => api.post<User>('/auth/register', body),
  logout: () => api.post<void>('/auth/logout'),
  /** 成功后后端为当前会话重新签发 Cookie，其他会话失效 */
  changePassword: (body: ChangePasswordRequest) => api.post<void>('/auth/change-password', body),
}
