import { api, request } from './client'
import type { AdminUser, PermissionDefinition, Role, RoleCreate, RoleList, RoleUpdate, UserQuota, UserQuotaUpdate } from './types'

export const roleApi = {
  permissions: (signal?: AbortSignal) => request<{ items: PermissionDefinition[] }>('/admin/permissions', { signal }),
  list: (query: { q?: string; page?: number; page_size?: number } = {}, signal?: AbortSignal) => request<RoleList>('/admin/roles', { query, signal }),
  get: (id: number, signal?: AbortSignal) => request<Role>(`/admin/roles/${id}`, { signal }),
  create: (body: RoleCreate, signal?: AbortSignal) => api.post<Role>('/admin/roles', body, signal),
  update: (id: number, body: RoleUpdate, signal?: AbortSignal) => request<Role>(`/admin/roles/${id}`, { method: 'PATCH', body, signal }),
  delete: (role: Pick<Role, 'id' | 'revision' | 'authorization_revision'>) => api.del(`/admin/roles/${role.id}`, { revision: role.revision, authorization_revision: role.authorization_revision }),
  assign: (userId: number, body: { authorization_revision: number; role_ids: number[] }, signal?: AbortSignal) => request<AdminUser>(`/admin/users/${userId}/roles`, { method: 'PUT', body, signal }),
  quotas: (userId: number, signal?: AbortSignal) => request<UserQuota>(`/admin/users/${userId}/quotas`, { signal }),
  updateQuotas: (userId: number, body: UserQuotaUpdate, signal?: AbortSignal) => request<UserQuota>(`/admin/users/${userId}/quotas`, { method: 'PATCH', body, signal }),
}
