import type { AdminUser, User } from '../api/types'
import type { AuthStatus } from '../stores/auth'

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
    canDelete: !isSelf,
  }
}

/** 删除确认：输入必须与用户名完全一致（不做 trim / 大小写归一）。 */
export function canConfirmDelete(input: string, username: string): boolean {
  return input === username
}
