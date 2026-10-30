import { api } from './client'
import type { ApiKey, ApiKeyCreate, ApiKeyCreated, ApiKeyRevealed, ApiKeyStatus, ApiKeyUpdate } from './types'

export const OPEN_API_DOCS_URL = '/api/open/v1/docs'
export const OPEN_API_SCHEMA_URL = '/api/open/v1/openapi.json'

export const API_KEY_STATUS: Record<ApiKeyStatus, { label: string; color: string }> = {
  active: { label: '可用', color: 'success' },
  expired: { label: '已过期', color: 'warning' },
  revoked: { label: '已撤销', color: 'default' },
}

/** 管理端只使用现有 Cookie 会话，不把平台 Key 作为登录凭据。 */
export const apiKeyApi = {
  list: () => api.get<ApiKey[]>('/api-keys'),
  create: (body: ApiKeyCreate) => api.post<ApiKeyCreated>('/api-keys', body),
  update: (id: number, body: ApiKeyUpdate) => api.patch<ApiKey>(`/api-keys/${id}`, body),
  reveal: (id: number, signal?: AbortSignal) => api.post<ApiKeyRevealed>(`/api-keys/${id}/reveal`, undefined, signal),
  revoke: (id: number) => api.post<void>(`/api-keys/${id}/revoke`),
}
