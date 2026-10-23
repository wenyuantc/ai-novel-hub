import { api } from './client'
import type { AuthConfig, Credentials, User } from './types'

export const authApi = {
  config: () => api.get<AuthConfig>('/auth/config'),
  me: () => api.get<User>('/auth/me'),
  login: (body: Credentials) => api.post<User>('/auth/login', body),
  register: (body: Credentials) => api.post<User>('/auth/register', body),
  logout: () => api.post<void>('/auth/logout'),
}
