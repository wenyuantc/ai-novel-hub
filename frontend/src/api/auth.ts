import { api } from './client'
import type { AuthConfig, ChangePasswordRequest, Credentials, User } from './types'

export const authApi = {
  config: () => api.get<AuthConfig>('/auth/config'),
  me: () => api.get<User>('/auth/me'),
  login: (body: Credentials) => api.post<User>('/auth/login', body),
  register: (body: Credentials) => api.post<User>('/auth/register', body),
  logout: () => api.post<void>('/auth/logout'),
  /** 成功后后端为当前会话重新签发 Cookie，其他会话失效 */
  changePassword: (body: ChangePasswordRequest) => api.post<void>('/auth/change-password', body),
}
