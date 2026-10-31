import type { AdminUser, User } from '../api/types'
import type { AuthStatus } from '../stores/auth'
import { ApiError } from '../api/client'

/** RequireAdmin 的判定：加载中 / 去登录 / 回首页 / 放行。 */
export type AdminGuardResult = 'loading' | 'login' | 'home' | 'allow'

export function adminGuard(status: AuthStatus, user: User | null): AdminGuardResult {
  if (status === 'unknown') return 'loading'
  if (status === 'anonymous' || !user) return 'login'
  return user.is_admin ? 'allow' : 'home'
}

export interface RowActions {
  isSelf: boolean
  canResetPassword: boolean
  canToggleActive: boolean
  canToggleAdmin: boolean
  canDelete: boolean
}

/**
 * 行操作可用性：对当前登录用户自己的行禁用危险操作（后端同样拒绝）。
 * 重置自己的密码会让当前会话失效，因此自己的行也不提供，改走「修改密码」。
 */
export function rowActions(row: AdminUser, currentUserId: number | undefined): RowActions {
  const isSelf = row.id === currentUserId
  return {
    isSelf,
    canResetPassword: !isSelf,
    canToggleActive: !isSelf,
    canToggleAdmin: !isSelf,
    canDelete: !isSelf && row.project_count === 0,
  }
}

/** 删除确认：输入必须与用户名完全一致（不做 trim / 大小写归一）。 */
export function canConfirmDelete(input: string, username: string): boolean {
  return input === username
}

export function userDeleteBlockReason(row: Pick<AdminUser, 'project_count'>): string | null {
  return row.project_count > 0 ? `仍有 ${row.project_count} 部作品（含回收站及待清理作品）。可先禁用账号；必须彻底清理全部作品后才能删除用户。` : null
}

/** 只接纳删号冲突中的安全计数，其他错误仍显示服务器 detail。 */
export function userProjectCountFromError(error: unknown): number | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null
  const value = error.body?.project_count
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null
}
