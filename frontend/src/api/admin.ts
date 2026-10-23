import { api } from './client'
import type { AdminUser, AdminUserCreate, AdminUserListQuery, AdminUserUpdate, Page, ResetPasswordRequest } from './types'

export const adminApi = {
  listUsers: ({ q, page, page_size }: AdminUserListQuery = {}) =>
    api.get<Page<AdminUser>>('/admin/users', { q: q?.trim() || undefined, page, page_size }),
  createUser: (body: AdminUserCreate) => api.post<AdminUser>('/admin/users', body),
  updateUser: (id: number, body: AdminUserUpdate) => api.patch<AdminUser>(`/admin/users/${id}`, body),
  /** 该用户所有已登录会话立即失效 */
  resetPassword: (id: number, body: ResetPasswordRequest) => api.post<void>(`/admin/users/${id}/reset-password`, body),
  deleteUser: (id: number) => api.del(`/admin/users/${id}`),
}
