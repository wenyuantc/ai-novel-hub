import { api } from './client'
import type { AdminProject, AdminProjectListQuery, AdminUser, AdminUserCreate, AdminUserListQuery, AdminUserUpdate, Page, ResetPasswordRequest } from './types'

export const adminApi = {
  listProjects: ({ q, owner_user_id, status, page, page_size }: AdminProjectListQuery = {}) =>
    api.get<Page<AdminProject>>('/admin/projects', { q: q?.trim() || undefined, owner_user_id, status, page, page_size }),
  listUsers: ({ q, page, page_size }: AdminUserListQuery = {}) =>
    api.get<Page<AdminUser>>('/admin/users', { q: q?.trim() || undefined, page, page_size }),
  createUser: (body: AdminUserCreate) => api.post<AdminUser>('/admin/users', body),
  updateUser: (id: number, body: AdminUserUpdate) => api.patch<AdminUser>(`/admin/users/${id}`, body),
  /** 该用户所有已登录会话立即失效 */
  resetPassword: (id: number, body: ResetPasswordRequest) => api.post<void>(`/admin/users/${id}/reset-password`, body),
  deleteUser: (id: number) => api.del(`/admin/users/${id}`),
}
